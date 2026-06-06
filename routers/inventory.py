import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
from core.config import TTL_MAX_HOURS
from core.deps import pve_client, tf_runner
from core.security import (
    UserSession,
    assert_vm_accessible_by_name,
    assert_vm_accessible_by_vmid,
    get_current_session,
)
from services.proxmox_client import ProxmoxIDPClient
from services.task_manager import TaskStatus, deployment_lock, tasks_db

router = APIRouter(prefix="/api", tags=["inventory"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class VMInventoryItem(BaseModel):
    vmid: int
    name: str
    node: str
    status: str
    ram_mb: int
    cpu: int
    managed: bool
    disk_gb: float
    sockets: int
    networks: List[dict]
    template_id: Optional[int] = None
    template_name: Optional[str] = None
    ttl_expires_at: Optional[float] = None   # epoch; None = persistent / not tracked


class PowerActionRequest(BaseModel):
    action: str
    vmid: int
    node_name: str


class UpdateTTLRequest(BaseModel):
    ttl_hours: Optional[int] = None   # None / 0 → make persistent ("Nunca")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_vm_details(
    vm_base: dict,
    client,
    template_map: dict,
) -> VMInventoryItem:
    is_managed = "managed-by-idp" in vm_base.get("tags", [])
    ram_mb = int(vm_base.get("maxmem", 0) / (1024 * 1024))
    disk_gb = vm_base.get("maxdisk", 0) / (1024 * 1024 * 1024)
    sockets = 1
    networks = []

    try:
        config = client.proxmox.nodes(vm_base["node"]).qemu(vm_base["vmid"]).config.get()
        sockets = int(config.get("sockets", 1))
        for key, value in config.items():
            if key.startswith("net"):
                parts = value.split(",")
                bridge = next(
                    (p.split("=")[1] for p in parts if p.startswith("bridge=")),
                    "unknown",
                )
                networks.append({"id": key, "bridge": bridge})
    except Exception:
        pass

    template_id = None
    template_name = None
    if is_managed:
        try:
            state_path = os.path.join(
                tf_runner.tf_directory,
                "terraform.tfstate.d",
                vm_base["name"],
                "terraform.tfstate",
            )
            if os.path.exists(state_path):
                with open(state_path) as f:
                    state = json.load(f)
                vm_resources = [
                    r for r in state.get("resources", [])
                    if r.get("type") == "proxmox_virtual_environment_vm"
                ]
                if vm_resources:
                    clone = vm_resources[0]["instances"][0]["attributes"].get("clone") or []
                    if clone:
                        template_id = clone[0].get("vm_id")
                        if template_id:
                            template_name = template_map.get(template_id, f"ID: {template_id}")
        except Exception:
            pass

    return VMInventoryItem(
        vmid=vm_base["vmid"],
        name=vm_base["name"],
        node=vm_base["node"],
        status=vm_base["status"],
        ram_mb=ram_mb,
        cpu=vm_base.get("maxcpu", 0),
        managed=is_managed,
        disk_gb=disk_gb,
        sockets=sockets,
        networks=networks,
        template_id=template_id,
        template_name=template_name,
    )


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get("/nodes", response_model=List[str])
def get_nodes(sess: UserSession = Depends(get_current_session)):
    return sess.client.get_nodes()


@router.get("/nodes/{node}/templates", response_model=Dict[str, int])
def get_templates(node: str, sess: UserSession = Depends(get_current_session)):
    return sess.client.get_templates(node)


@router.get("/nodes/{node}/networks")
def get_networks(node: str, sess: UserSession = Depends(get_current_session)):
    return sess.client.get_networks(node)


@router.get("/vmid/next", response_model=int)
def get_next_vmid(sess: UserSession = Depends(get_current_session)):
    return sess.client.get_next_vmid()


@router.get("/inventory/metrics")
def get_inventory_metrics(sess: UserSession = Depends(get_current_session)):
    """
    Ultra-lightweight live metrics. Single Proxmox API call per request — no N+1.
    RBAC enforced via impersonated client: only VMs visible to the caller are included.
    Returns: {"<vmid>": {"cpu_pct": float, "mem_pct": float, "status": str}, ...}
    """
    return sess.client.get_vm_metrics()


@router.get("/inventory", response_model=List[VMInventoryItem])
def get_inventory(sess: UserSession = Depends(get_current_session)):
    raw_vms = sess.client.get_inventory()
    template_map = sess.client.get_template_names()
    result = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [
            executor.submit(_fetch_vm_details, vm, sess.client, template_map)
            for vm in raw_vms
        ]
        for future in as_completed(futures):
            result.append(future.result())
    result.sort(key=lambda x: x.vmid)

    # Enrich with ephemeral TTL info (single DB read, mapped by vm_name).
    expiry_map = database.get_expiry_map()
    for item in result:
        item.ttl_expires_at = expiry_map.get(item.name)

    return result


@router.put("/inventory/{vmid}/ttl")
def update_vm_ttl(
    vmid: int,
    body: UpdateTTLRequest,
    sess: UserSession = Depends(get_current_session),
):
    """Renew a VM's TTL or convert it to persistent. RBAC: only the caller's VMs."""
    # Single inventory read doubles as the RBAC gate and the vm_name/node lookup.
    vm = next((v for v in sess.client.get_inventory() if v.get("vmid") == vmid), None)
    if vm is None:
        raise HTTPException(
            status_code=403, detail=f"Acceso denegado: la VM {vmid} no está en tu inventario"
        )

    pool_name = ProxmoxIDPClient._poolid_for(sess.userid)

    if body.ttl_hours:
        if body.ttl_hours < 1 or body.ttl_hours > TTL_MAX_HOURS:
            raise HTTPException(
                status_code=400,
                detail=f"TTL fuera de rango: debe estar entre 1 y {TTL_MAX_HOURS} horas",
            )
        expires_at = time.time() + body.ttl_hours * 3600
        database.register_lifecycle(
            vm["name"], vmid, vm["node"], sess.userid, pool_name, expires_at
        )
        return {"message": "TTL actualizado", "ttl_expires_at": expires_at}

    # None / 0 → mark persistent (keep the row for auditability, expires_at NULL).
    database.register_lifecycle(
        vm["name"], vmid, vm["node"], sess.userid, pool_name, None
    )
    return {"message": "VM marcada como persistente", "ttl_expires_at": None}


@router.post("/action")
def power_action(
    request: PowerActionRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, request.vmid)
    try:
        pve_client.set_vm_power_state(request.node_name, request.vmid, request.action)
        return {"message": f"Orden '{request.action}' enviada con éxito a la VM {request.vmid}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/audit/{vm_name}")
def audit_infrastructure(vm_name: str, sess: UserSession = Depends(get_current_session)):
    assert_vm_accessible_by_name(sess, vm_name)
    try:
        with deployment_lock:
            result = tf_runner.audit(vm_name)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Task stream ───────────────────────────────────────────────────────────────

@router.get("/tasks")
def get_tasks(sess: UserSession = Depends(get_current_session)):
    """Returns active tasks owned by the caller plus ownerless (system) tasks."""
    current_time = time.time()
    active_tasks = []
    keys_to_delete = []

    for t_id, task in tasks_db.items():
        if task["status"] in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
            if current_time - task.get("updated_at", current_time) > 30:
                keys_to_delete.append(t_id)
                continue

        task_owner = task.get("owner")
        if task_owner is not None and task_owner != sess.userid:
            continue

        active_tasks.append(task)

    for k in keys_to_delete:
        del tasks_db[k]

    return active_tasks
