import os
import re
import time
import urllib3
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ProxmoxIDPClient:
    """
    API client to interact with Proxmox.
    Serves as an abstraction layer for the IDP portal.

    Two construction paths:
    - ProxmoxIDPClient()           → global API token (privileged, for Terraform ops)
    - ProxmoxIDPClient.from_user() → per-user ticket (impersonation, for RBAC reads)
    """
    def __init__(self):
        load_dotenv()
        
        url = os.getenv("PROXMOX_URL")
        full_token_id = os.getenv("PROXMOX_TOKEN_ID")
        secret_token = os.getenv("PROXMOX_SECRET_TOKEN")

        if not all([url, full_token_id, secret_token]):
            raise ValueError(" Missing credentials in the .env file")

        host = url.replace("https://", "").split(":")[0]

        if "!" not in full_token_id:
            raise ValueError("PROXMOX_TOKEN_ID must include a '!' (e.g., user@pve!token)")

        user, token_name = full_token_id.split("!")

        try:
            self.proxmox = ProxmoxAPI(
                host,
                user=user,
                token_name=token_name,
                token_value=secret_token,
                verify_ssl=False
            )
        except Exception as e:
            raise ConnectionError(f" Error connecting to Proxmox: {e}")

    @classmethod
    def from_user(cls, username: str, realm: str, password: str) -> "ProxmoxIDPClient":
        """
        Creates a client authenticated as a specific Proxmox user via ticket (impersonation).
        Used for all RBAC read operations so Proxmox filters data natively.

        The underlying ProxmoxHTTPAuth object auto-renews the ticket every 3600s while
        the client is alive — no password is stored server-side after this call.

        Raises proxmoxer.core.AuthenticationError (a subclass of Exception) if credentials
        are wrong or the user has no access to Proxmox.
        """
        load_dotenv()
        url = os.getenv("PROXMOX_URL", "")
        host = url.replace("https://", "").split(":")[0]

        instance = cls.__new__(cls)  # bypass __init__ to skip token-based setup
        instance.proxmox = ProxmoxAPI(
            host,
            user=f"{username}@{realm}",
            password=password,
            verify_ssl=False
        )
        return instance

    # ------------------------------------------------------------------ #
    #  Privileged operations (called by the global-token client only)     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _poolid_for(userid: str) -> str:
        """
        Sanitize a Proxmox userid (e.g. 'daniel@pve') into a valid pool ID.
        Pool IDs must be alphanumeric + hyphens (no @ ! = spaces).
        Result: 'idp-daniel-pve'
        """
        sanitized = re.sub(r"[^a-zA-Z0-9\-]", "-", userid)
        return f"idp-{sanitized}"

    def add_managed_tag(self, node: str, vmid: int) -> None:
        """
        Adds 'managed-by-idp' to the VM's tag list without removing existing tags.
        Must be called with the GLOBAL token client (write access to VM config).
        Handles both ';' (PVE 7.3+) and ',' (legacy) tag separators.
        """
        config = self.proxmox.nodes(node).qemu(vmid).config.get()
        raw_tags = config.get('tags', '') or ''
        # Normalize to comma-split regardless of separator, then write back with ';'
        tag_list = [t.strip() for t in raw_tags.replace(';', ',').split(',') if t.strip()]
        if 'managed-by-idp' not in tag_list:
            tag_list.append('managed-by-idp')
        self.proxmox.nodes(node).qemu(vmid).config.put(tags=';'.join(tag_list))

    def ensure_user_pool(self, userid: str) -> str:
        """
        Idempotently creates the user's Resource Pool and grants PVEVMAdmin on it.
        Must be called with the GLOBAL token client (not the impersonated one).

        Returns the pool ID (e.g. 'idp-daniel-pve').
        """
        poolid = self._poolid_for(userid)

        existing_pools = {p["poolid"] for p in self.proxmox.pools.get()}
        if poolid not in existing_pools:
            self.proxmox.pools.post(poolid=poolid, comment=f"IDP owner pool for {userid}")

        # Assign PVEVMAdmin role on the pool with propagation (idempotent PUT)
        self.proxmox.access.acl.put(
            path=f"/pool/{poolid}",
            roles="PVEVMAdmin",
            users=userid,
            propagate=1,
        )
        return poolid

    def get_nodes(self) -> list:
        """Returns a list with the names of active nodes."""
        nodes = self.proxmox.nodes.get()
        return sorted([node['node'] for node in nodes if node['status'] == 'online'])

    def get_networks(self, node: str) -> list:
        """
        Returns a combined list of the node's local Bridges and cluster VNets (SDN).
        """
        available_networks = {"bridges": set(), "vnets": set()}

        try:
            interfaces = self.proxmox.nodes(node).network.get()
            for net in interfaces:
                if net.get('type') in ['bridge', 'OVSBridge']:
                    available_networks['bridges'].add(net['iface'])
        except Exception as e:
            print(f" Warning getting networks from node {node}: {e}")

        try:
            vnets = self.proxmox.cluster.sdn.vnets.get()
            for vnet in vnets:
                available_networks['vnets'].add(vnet['vnet'])
        except Exception as e:
            print(f" Warning getting VNets from SDN: {e}")

        return {
            "bridges": sorted(list(available_networks["bridges"])),
            "vnets": sorted(list(available_networks["vnets"]))
        }

    def get_templates(self, node: str) -> dict:
        available_templates = {}
        try:
            vms = self.proxmox.nodes(node).qemu.get()
            
            for vm in vms:
                if vm.get('template') == 1:
                    name = vm.get('name', 'Unnamed Template')
                    vmid = vm.get('vmid')
                    label = f"{name} (ID: {vmid})"
                    
                    available_templates[label] = vmid
                    
            return available_templates
        except Exception as e:
            print(f"Error getting templates from node {node}: {e}")
            return {}

    def get_next_vmid(self) -> int:
        """Gets the next free virtual machine ID in the cluster."""
        try:
            return self.proxmox.cluster.nextid.get()
        except Exception as e:
            print(f"Error getting the next VMID: {e}")
            return -1

    def get_template_names(self) -> dict:
        """
        Returns {vmid: name} for every VM template visible to this client.
        Used to resolve template_id → human-readable name in the inventory.
        Falls back to an empty dict on any error so the inventory degrades gracefully.
        """
        try:
            resources = self.proxmox.cluster.resources.get(type='vm')
            return {
                res['vmid']: res.get('name', f'template-{res["vmid"]}')
                for res in resources
                if res.get('type') == 'qemu' and res.get('template') == 1
            }
        except Exception:
            return {}
        
    def get_inventory(self) -> list:
        """
        Scans the entire cluster looking for virtual machines (QEMU).
        Returns metadata and tags to classify them.
        """
        try:
            resources = self.proxmox.cluster.resources.get(type='vm')
            inventory = []
            
            for res in resources:
                if res.get('type') == 'qemu':
                    raw_tags = res.get('tags', '')
                    tags = [t.strip() for t in raw_tags.split(',')] if raw_tags else []
                    
                    inventory.append({
                        "vmid": res.get("vmid"),
                        "name": res.get("name", "Unknown"),
                        "node": res.get("node"),
                        "status": res.get("status"),
                        "maxmem": res.get("maxmem", 0),
                        "maxcpu": res.get("maxcpu", 0),
                        "maxdisk": res.get("maxdisk", 0),
                        "tags": tags
                    })
            return inventory
            
        except Exception as e:
            print(f"❌ Error scanning cluster inventory: {e}")
            return []
        
    def set_vm_power_state(self, node: str, vmid: int, action: str) -> bool:
        """
        Changes the power state and WAITS until both the local node and 
        the global cluster cache are synchronized with the new state.
        """
        try:
            target_status = 'running' if action == 'start' else 'stopped'

            if action == 'start':
                self.proxmox.nodes(node).qemu(vmid).status.start.post()
            elif action == 'stop':
                self.proxmox.nodes(node).qemu(vmid).status.stop.post()
            else:
                raise ValueError("Unsupported action. Use 'start' or 'stop'.")

            local_synced = False
            for _ in range(30):
                current_info = self.proxmox.nodes(node).qemu(vmid).status.current.get()
                if current_info.get("status") == target_status:
                    local_synced = True
                    break
                time.sleep(2)
                
            if not local_synced:
                print(f"Warning: Timeout waiting for local VM {vmid} to change to {target_status}")
                return True

            for _ in range(10):
                resources = self.proxmox.cluster.resources.get(type='vm')
                
                vm_in_cluster = next((res for res in resources if res.get('vmid') == vmid), None)
                
                if vm_in_cluster and vm_in_cluster.get('status') == target_status:
                    return True
                    
                time.sleep(2)
                
            print(f"Warning: Timeout waiting for cluster cache to sync for VM {vmid}")
            return True
            
        except Exception as e:
            print(f"Error changing power state ({action}) on VM {vmid}: {e}")
            raise e
    
    def get_vm_config(self, node: str, vmid: int) -> dict:
        """Get the raw configuration file of the VM to view netX and sockets."""
        return self.proxmox.nodes(node).qemu(vmid).config.get()

    # ------------------------------------------------------------------ #
    #  Snapshot operations (direct Proxmox API — no Terraform)            #
    # ------------------------------------------------------------------ #

    def _wait_for_proxmox_task(self, node: str, upid: str, timeout: int = 300) -> None:
        """
        Polls a Proxmox task UPID until it reaches 'stopped' status.
        Raises RuntimeError if the task fails, TimeoutError if it exceeds `timeout` seconds.
        All snapshot write operations (create/rollback/delete) return a UPID and must be polled.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.proxmox.nodes(node).tasks(upid).status.get()
            if result.get("status") == "stopped":
                exit_status = result.get("exitstatus", "unknown")
                if exit_status != "OK":
                    raise RuntimeError(f"Proxmox task failed [{exit_status}]")
                return
            time.sleep(3)
        raise TimeoutError(f"Proxmox task timed out after {timeout}s (UPID: {upid})")

    def get_snapshots(self, node: str, vmid: int) -> list:
        """
        Returns the snapshot list for a VM, sorted newest-first.
        The 'current' pseudo-snapshot (Proxmox's pointer to live state) is excluded —
        it is not a restorable point and has no snaptime.
        """
        raw = self.proxmox.nodes(node).qemu(vmid).snapshot.get()
        snaps = []
        for s in raw:
            if s.get("name") == "current":
                continue
            snaps.append({
                "name":        s.get("name"),
                "description": s.get("description", ""),
                "snaptime":    s.get("snaptime"),     # Unix timestamp, None if missing
                "parent":      s.get("parent", ""),
                "vmstate":     bool(s.get("vmstate", 0)),  # True = RAM included
            })
        snaps.sort(key=lambda x: x.get("snaptime") or 0, reverse=True)
        return snaps

    def create_snapshot(self, node: str, vmid: int, snapname: str, description: str = "") -> None:
        """Creates a VM snapshot and blocks until the Proxmox task completes."""
        upid = self.proxmox.nodes(node).qemu(vmid).snapshot.post(
            snapname=snapname,
            description=description,
        )
        self._wait_for_proxmox_task(node, upid)

    def rollback_snapshot(self, node: str, vmid: int, snapname: str) -> None:
        """
        Rolls the VM back to the given snapshot.
        NOTE: Proxmox requires the VM to be stopped for a full rollback unless
        qemu-guest-agent is active and the snapshot did not include RAM state.
        """
        upid = self.proxmox.nodes(node).qemu(vmid).snapshot(snapname).rollback.post()
        self._wait_for_proxmox_task(node, upid)

    def delete_snapshot(self, node: str, vmid: int, snapname: str) -> None:
        """Deletes a snapshot and blocks until the Proxmox task completes."""
        upid = self.proxmox.nodes(node).qemu(vmid).snapshot(snapname).delete()
        self._wait_for_proxmox_task(node, upid)

    # ------------------------------------------------------------------ #
    #  Quota helpers (called with the GLOBAL token client)                #
    # ------------------------------------------------------------------ #

    def get_pool_usage(self, pool_name: str) -> dict:
        """
        Returns live CPU/RAM/Disk consumption for every QEMU VM in `pool_name`.
        Uses GET /pools/{pool_name} which is the authoritative member list.
        Returns {"cpu": int, "ram_mb": int, "disk_gb": float} — zeros if pool
        does not exist yet (user hasn't deployed anything).
        """
        try:
            pool_data = self.proxmox.pools(pool_name).get()
            members = pool_data.get("members", [])
            qemu = [m for m in members if m.get("type") == "qemu"]
            return {
                "cpu":     sum(m.get("maxcpu", 0) for m in qemu),
                "ram_mb":  sum(int(m.get("maxmem", 0) / (1024 * 1024)) for m in qemu),
                "disk_gb": round(
                    sum(m.get("maxdisk", 0) / (1024 * 1024 * 1024) for m in qemu), 2
                ),
            }
        except Exception:
            return {"cpu": 0, "ram_mb": 0, "disk_gb": 0.0}

    def get_all_pools(self) -> list:
        """Returns all pool IDs visible to this client (admin token)."""
        try:
            return [p["poolid"] for p in self.proxmox.pools.get()]
        except Exception:
            return []

    def get_vm_resource(self, vmid: int) -> dict | None:
        """
        Returns the live cluster.resources entry for a QEMU VM (including its
        'name' and 'pool'), or None if it no longer exists. Single API call —
        used by the TTL reaper to cross-verify a VM before destroying it.
        """
        try:
            resources = self.proxmox.cluster.resources.get(type="vm")
            return next(
                (
                    r for r in resources
                    if r.get("vmid") == vmid and r.get("type") == "qemu"
                ),
                None,
            )
        except Exception:
            return None

    def open_vncproxy(self, node: str, vmid: int) -> dict:
        return self.proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)

    def open_termproxy(self, node: str, vmid: int) -> dict:
        return self.proxmox.nodes(node).qemu(vmid).termproxy.post()

    def has_serial_console(self, node: str, vmid: int) -> bool:
        try:
            cfg = self.get_vm_config(node, vmid)
            return "serial0" in cfg
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  QEMU Guest Agent (instalación de apps post-arranque, solo API)      #
    # ------------------------------------------------------------------ #

    def agent_wait_ready(self, node: str, vmid: int, timeout: int = 300) -> bool:
        """
        Espera a que el qemu-guest-agent responda (VM arrancada + agente vivo).
        Devuelve True si responde antes de `timeout`, False en caso contrario.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.proxmox.nodes(node).qemu(vmid).agent.ping.post()
                return True
            except Exception:
                time.sleep(5)
        return False

    def agent_os_family(self, node: str, vmid: int) -> str | None:
        """
        Devuelve la familia de SO ('debian'|'rhel'|'freebsd') vía get-osinfo del agente,
        o None si no se puede determinar / no está soportada.
        """
        from services import tool_catalog
        try:
            info = self.proxmox.nodes(node).qemu(vmid).agent("get-osinfo").get()
            result = info.get("result", info) if isinstance(info, dict) else {}
            return tool_catalog.os_family_from_id(result.get("id", ""))
        except Exception:
            return None

    def agent_run(self, node: str, vmid: int, command: str, timeout: int = 900) -> tuple:
        """
        Ejecuta `command` dentro de la VM vía guest-agent (envuelto en `/bin/sh -lc`),
        sondeando exec-status hasta que termina. Devuelve (exitcode, out, err).
        Lanza TimeoutError si excede `timeout`.
        """
        agent = self.proxmox.nodes(node).qemu(vmid).agent
        res = agent.exec.post(command=["/bin/sh", "-lc", command])
        pid = res.get("pid")

        deadline = time.time() + timeout
        while time.time() < deadline:
            status = agent("exec-status").get(pid=pid)
            if status.get("exited"):
                return (
                    status.get("exitcode", 1),
                    status.get("out-data", "") or "",
                    status.get("err-data", "") or "",
                )
            time.sleep(3)
        raise TimeoutError(f"Comando guest-agent excedió {timeout}s en VM {vmid}")

    def get_vm_metrics(self) -> dict:
        """
        Live CPU / RAM metrics for all VMs visible to this client. One API call — no N+1.

        cluster.resources already carries:
          cpu    — float 0.0–1.0 (current usage / allocated vCPUs)
          mem    — bytes currently consumed
          maxmem — bytes allocated (RAM ceiling for this VM)

        RBAC is enforced by the caller's client: impersonated clients receive only
        the VMs Proxmox would show to that user in the UI.

        Returns:
            {"<vmid_str>": {"cpu_pct": float, "mem_pct": float, "status": str}, ...}
        """
        try:
            resources = self.proxmox.cluster.resources.get(type="vm")
            result: dict = {}
            for res in resources:
                if res.get("type") != "qemu":
                    continue
                maxmem = res.get("maxmem", 0)
                result[str(res["vmid"])] = {
                    "cpu_pct": round(res.get("cpu", 0.0) * 100, 1),
                    "mem_pct": round(res.get("mem", 0) / maxmem * 100, 1) if maxmem > 0 else 0.0,
                    "status":  res.get("status", "unknown"),
                }
            return result
        except Exception:
            return {}