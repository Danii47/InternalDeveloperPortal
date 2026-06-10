import logging
import os
import re
import time
import urllib3
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv

from core.config import proxmox_tls_verify

# Solo silencia el warning de TLS cuando NO se verifica (comportamiento por defecto).
if proxmox_tls_verify() is False:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("proxmox_client")

# Tipos de zona SDN cuyas VNets EXIGEN un tag (VLAN id / VNI). 'simple' y casos desconocidos no.
_TAG_ZONES = ("vlan", "vxlan", "qinq", "evpn")

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
                verify_ssl=proxmox_tls_verify()
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
            verify_ssl=proxmox_tls_verify()
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

    def pool_exists(self, poolid: str) -> bool:
        try:
            return poolid in {p["poolid"] for p in self.proxmox.pools.get()}
        except Exception:
            return False

    def grant_acl(self, path: str, role: str, groups=None, users=None, propagate: int = 1) -> None:
        """
        Concede `role` en `path` a uno o varios grupos y/o usuarios (PUT /access/acl).
        `groups`/`users` aceptan str o lista. Ej.: grant_acl('/sdn/zones/z/vnet', 'PVESDNUser', groups='g1').
        """
        def _csv(v):
            if not v:
                return None
            return v if isinstance(v, str) else ",".join(v)
        kwargs = {"path": path, "roles": role, "propagate": propagate}
        g, u = _csv(groups), _csv(users)
        if g:
            kwargs["groups"] = g
        if u:
            kwargs["users"] = u
        if g or u:
            self.proxmox.access.acl.put(**kwargs)

    def remove_acl(self, path: str, role: str, groups=None, users=None) -> None:
        """
        Revoca `role` en `path` para grupos/usuarios (PUT /access/acl con delete=1). Gemelo de
        `grant_acl`; se usa para limpiar ACL colgantes al borrar un pool o grupo.
        """
        def _csv(v):
            if not v:
                return None
            return v if isinstance(v, str) else ",".join(v)
        kwargs = {"path": path, "roles": role, "propagate": 0, "delete": 1}
        g, u = _csv(groups), _csv(users)
        if g:
            kwargs["groups"] = g
        if u:
            kwargs["users"] = u
        if g or u:
            self.proxmox.access.acl.put(**kwargs)

    def create_pool(self, poolid: str, comment: str = "", users=None, groups=None,
                    roles: str = "PVEVMAdmin") -> str:
        """
        Crea (idempotente) un Resource Pool y, opcionalmente, concede `roles` a `users`/`groups`
        sobre él con propagación. Cliente GLOBAL (token admin). Devuelve el poolid.
        """
        if not self.pool_exists(poolid):
            self.proxmox.pools.post(poolid=poolid, comment=comment)
        if users or groups:
            self.grant_acl(f"/pool/{poolid}", roles, groups=groups, users=users)
        return poolid

    def delete_pool(self, poolid: str) -> None:
        """Elimina un Resource Pool (debe estar vacío). Compensador de create_pool."""
        self.proxmox.pools(poolid).delete()

    # ------------------------------------------------------------------ #
    #  Grupos y permisos (modelo de inquilino de Blueprints)              #
    # ------------------------------------------------------------------ #

    def group_exists(self, groupid: str) -> bool:
        try:
            return groupid in {g["groupid"] for g in self.proxmox.access.groups.get()}
        except Exception:
            return False

    def create_group(self, groupid: str, comment: str = "") -> str:
        """Crea (idempotente) un grupo de Proxmox. Devuelve el groupid."""
        if not self.group_exists(groupid):
            self.proxmox.access.groups.post(groupid=groupid, comment=comment)
        return groupid

    def delete_group(self, groupid: str) -> None:
        """Elimina un grupo. Compensador de create_group."""
        self.proxmox.access.groups(groupid).delete()

    def get_groups(self) -> list:
        """Grupos del clúster: [{groupid, comment, users}]. Read-only. [] ante error."""
        try:
            return self.proxmox.access.groups.get() or []
        except Exception:
            return []

    def get_acls(self) -> list:
        """ACLs del clúster: [{path, roleid, type, ugid, propagate}]. Read-only. [] ante error."""
        try:
            return self.proxmox.access.acl.get() or []
        except Exception:
            return []

    def get_users(self) -> list:
        """Usuarios del clúster: [{userid, enable, groups, ...}]. Read-only. [] ante error."""
        try:
            return self.proxmox.access.users.get() or []
        except Exception:
            return []

    def _user_groups(self, userid: str) -> list:
        """Grupos actuales de un usuario (la membresía se modela en el usuario en Proxmox)."""
        try:
            g = self.proxmox.access.users(userid).get().get("groups")
            if isinstance(g, str):
                return [x for x in g.split(",") if x]
            return list(g or [])
        except Exception:
            return []

    def add_user_to_group(self, userid: str, groupid: str) -> None:
        """Añade `userid` a `groupid` (PUT /access/users/{id} con la lista de grupos actualizada)."""
        groups = self._user_groups(userid)
        if groupid not in groups:
            groups.append(groupid)
        self.proxmox.access.users(userid).put(groups=",".join(groups))

    def remove_user_from_group(self, userid: str, groupid: str) -> None:
        """Quita `userid` de `groupid`."""
        groups = [g for g in self._user_groups(userid) if g != groupid]
        self.proxmox.access.users(userid).put(groups=",".join(groups))

    def get_permissions(self) -> dict:
        """
        Permisos efectivos del token actual: {path: {priv: 1, ...}}. Read-only.
        Usado por el pre-flight para avisar de privilegios ausentes antes de ejecutar.
        Devuelve {} ante error.
        """
        try:
            return self.proxmox.access.permissions.get() or {}
        except Exception:
            return {}

    def ensure_user_pool(self, userid: str) -> str:
        """
        Idempotently creates the user's Resource Pool and grants PVEVMAdmin on it.
        Must be called with the GLOBAL token client (not the impersonated one).

        Returns the pool ID (e.g. 'idp-daniel-pve').
        """
        poolid = self._poolid_for(userid)
        return self.create_pool(poolid, comment=f"IDP owner pool for {userid}", users=userid)

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
            logger.warning("Error obteniendo redes del nodo %s: %s", node, e)

        try:
            vnets = self.proxmox.cluster.sdn.vnets.get()
            for vnet in vnets:
                available_networks['vnets'].add(vnet['vnet'])
        except Exception as e:
            logger.warning("Error obteniendo VNets de SDN: %s", e)

        return {
            "bridges": sorted(list(available_networks["bridges"])),
            "vnets": sorted(list(available_networks["vnets"]))
        }

    # ------------------------------------------------------------------ #
    #  SDN: zonas / VNets / subredes (usado por Blueprints, token admin)   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _first_host(cidr: str) -> str:
        """Primera IP utilizable de un CIDR (gateway por convención)."""
        import ipaddress
        net = ipaddress.ip_network(cidr, strict=False)
        return str(next(net.hosts()))

    def apply_sdn(self) -> None:
        """Aplica/recarga la configuración SDN pendiente (equivale a 'Apply' en la UI)."""
        self.proxmox.cluster.sdn.put()

    def get_sdn_zones(self) -> list:
        """Zonas SDN existentes: [{'zone','type'}]. Los blueprints NO crean zonas (solo se eligen)."""
        try:
            return [{"zone": z.get("zone"), "type": z.get("type") or ""}
                    for z in self.proxmox.cluster.sdn.zones.get()]
        except Exception:
            return []

    @staticmethod
    def _norm_tag(value) -> int:
        """Normaliza un tag de entrada (str/int/None/'') → int o None si no se indicó."""
        if value is None:
            return None
        s = str(value).strip()
        if s in ("", "None"):
            return None
        return int(s)

    def _next_free_tag(self, zone: str, zone_type: str) -> int:
        """Elige un tag libre en la zona. VLAN: 2-4094; VXLAN/QinQ (VNI): 1-16777215."""
        try:
            used = {
                int(v["tag"]) for v in self.proxmox.cluster.sdn.vnets.get()
                if v.get("zone") == zone and str(v.get("tag", "")).strip() not in ("", "None")
            }
        except Exception:
            used = set()
        lo, hi = (2, 4094) if zone_type == "vlan" else (1, 16777215)
        cand = max([lo - 1, *used]) + 1
        if cand > hi:  # raro: rango agotado por arriba → busca el primer hueco
            cand = next((t for t in range(lo, hi + 1) if t not in used), lo)
        return cand

    def create_vnet(self, vnet: str, zone: str = "idpzone",
                    subnet: str = None, gateway: str = None, tag=None, alias=None) -> dict:
        """
        Crea (idempotente) una VNet en una zona SDN **ya existente** (los blueprints no crean
        zonas) y, si se indica `subnet` (CIDR), su subred con gateway (primer host por defecto).
        `alias` fija el nombre descriptivo de la VNet. Las zonas vlan/vxlan/qinq/evpn EXIGEN un
        `tag` (VLAN id / VNI): si no se pasa, se autoasigna uno libre. Aplica SDN.
        Devuelve {'vnet','zone','subnet','tag','alias'} para auditoría/rollback.
        """
        zones = {z["zone"]: z["type"] for z in self.get_sdn_zones()}
        if zone not in zones:
            raise ValueError(
                f"La zona SDN '{zone}' no existe. Créala en Proxmox (Datacenter → SDN → Zones); "
                "los blueprints no crean zonas, solo VNets dentro de una zona existente."
            )
        zone_type = zones[zone]

        existing_vnets = {v["vnet"] for v in self.proxmox.cluster.sdn.vnets.get()}
        assigned_tag = self._norm_tag(tag)
        alias = (str(alias).strip() or None) if alias is not None else None
        if vnet not in existing_vnets:
            kwargs = {"vnet": vnet, "zone": zone}
            if alias:
                kwargs["alias"] = alias
            if zone_type in _TAG_ZONES:
                # vlan/vxlan/qinq/evpn EXIGEN un tag (VLAN id / VNI). Si no se indica, autoasignamos.
                if assigned_tag is None:
                    assigned_tag = self._next_free_tag(zone, zone_type)
                kwargs["tag"] = assigned_tag
            else:
                assigned_tag = None  # zonas 'simple' no llevan tag; se ignora si se hubiera pasado
            self.proxmox.cluster.sdn.vnets.post(**kwargs)

        if subnet:
            gw = gateway or self._first_host(subnet)
            try:
                existing_subnets = {
                    (s.get("cidr") or s.get("subnet"))
                    for s in self.proxmox.cluster.sdn.vnets(vnet).subnets.get()
                }
            except Exception:
                existing_subnets = set()
            if subnet not in existing_subnets:
                self.proxmox.cluster.sdn.vnets(vnet).subnets.post(
                    subnet=subnet, type="subnet", gateway=gw
                )

        self.apply_sdn()
        return {"vnet": vnet, "zone": zone, "subnet": subnet, "tag": assigned_tag, "alias": alias}

    def delete_vnet(self, vnet: str) -> None:
        """Elimina una VNet y recarga SDN. Compensador de create_vnet."""
        try:
            self.proxmox.cluster.sdn.vnets(vnet).delete()
        finally:
            self.apply_sdn()

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
            logger.error("Error obteniendo plantillas del nodo %s: %s", node, e)
            return {}

    def get_next_vmid(self) -> int:
        """Siguiente VMID libre del clúster. La API lo devuelve como string ('106') → lo casteamos
        a int para poder operar aritméticamente (p.ej. base_id + i en despliegues múltiples)."""
        try:
            return int(self.proxmox.cluster.nextid.get())
        except Exception as e:
            logger.error("Error obteniendo el siguiente VMID: %s", e)
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
            logger.error("Error escaneando el inventario del clúster: %s", e)
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
                logger.warning("Timeout esperando que la VM %s pase a %s (nodo local)", vmid, target_status)
                return True

            for _ in range(10):
                resources = self.proxmox.cluster.resources.get(type='vm')
                
                vm_in_cluster = next((res for res in resources if res.get('vmid') == vmid), None)
                
                if vm_in_cluster and vm_in_cluster.get('status') == target_status:
                    return True
                    
                time.sleep(2)
                
            logger.warning("Timeout esperando sincronización de la caché del clúster para la VM %s", vmid)
            return True
            
        except Exception as e:
            logger.error("Error cambiando el estado de energía (%s) de la VM %s: %s", action, vmid, e)
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

    def get_pool_members(self, pool_name: str) -> list:
        """Miembros de un pool (`GET /pools/{id}`.members) o [] si no existe / error."""
        try:
            return self.proxmox.pools(pool_name).get().get("members", []) or []
        except Exception:
            return []

    def get_all_pools(self) -> list:
        """Returns all pool IDs visible to this client (admin token)."""
        try:
            return [p["poolid"] for p in self.proxmox.pools.get()]
        except Exception:
            return []

    def get_vm_by_name(self, name: str) -> dict | None:
        """
        Busca una VM QEMU por nombre en el clúster. Devuelve {'vmid','node','name'} o None.
        Usado por las acciones de Blueprint que apuntan a una VM por nombre.
        """
        try:
            resources = self.proxmox.cluster.resources.get(type="vm")
            r = next(
                (x for x in resources
                 if x.get("type") == "qemu" and x.get("name") == name),
                None,
            )
            return {"vmid": r["vmid"], "node": r["node"], "name": r["name"]} if r else None
        except Exception:
            return None

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

    # Comando de prueba de salida a internet: prueba HTTPS con curl/wget y, si no
    # están disponibles, ICMP con ping. Éxito (exit 0) en cualquiera de las tres = hay internet.
    _INTERNET_CHECK_CMD = (
        "(command -v curl >/dev/null 2>&1 && curl -fsS -m5 -o /dev/null https://1.1.1.1) || "
        "(command -v wget >/dev/null 2>&1 && wget -q -T5 -O /dev/null https://1.1.1.1) || "
        "(command -v ping >/dev/null 2>&1 && ping -c1 -W3 1.1.1.1 >/dev/null 2>&1)"
    )

    def agent_check_internet(self, node: str, vmid: int, timeout: int = 30) -> bool:
        """
        Comprueba si la VM tiene salida a internet ejecutando una petición de prueba
        (curl/wget/ping a 1.1.1.1) dentro de la VM vía guest-agent. Devuelve False si
        el comando falla, excede `timeout` o ninguna herramienta está disponible.
        """
        try:
            code, _, _ = self.agent_run(node, vmid, self._INTERNET_CHECK_CMD, timeout=timeout)
            return code == 0
        except Exception:
            return False

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