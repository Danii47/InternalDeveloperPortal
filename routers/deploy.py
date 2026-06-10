import ipaddress
import json
import os
import re
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import database
from core.config import TTL_MAX_HOURS
from core.deps import pve_client, tf_runner
from services import app_installer, connectivity, tool_catalog, validation
from core.security import (
    UserSession,
    assert_deploy_params_allowed,
    assert_vm_accessible_by_name,
    assert_vm_accessible_by_vmid,
    get_current_session,
)
from services.proxmox_client import ProxmoxIDPClient
from services.task_manager import create_and_queue_task, run_unlocked_task
from services.terraform_runner import VMConfig

router = APIRouter(prefix="/api", tags=["deploy"])


# ── Pydantic models ───────────────────────────────────────────────────────────

_VM_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,62}$")
_SNAPNAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,40}$")


def _check_vm_name(v: str) -> str:
    if not _VM_NAME_RE.match(v):
        raise ValueError(
            "El nombre de VM solo puede contener letras, números, puntos y guiones, "
            "debe empezar por letra o número, y tener máximo 63 caracteres"
        )
    return v


def _check_snapname_param(snapname: str) -> None:
    if not _SNAPNAME_RE.match(snapname):
        raise HTTPException(
            status_code=400,
            detail="Nombre de snapshot inválido: solo letras, números, guiones y guiones bajos (máx. 40 caracteres)",
        )


class Nic(BaseModel):
    """Una interfaz de red con su configuración IP propia."""
    bridge: str
    ip_mode: str = "dhcp"            # "dhcp" | "static"
    ip: Optional[str] = None         # IP inicial en CIDR cuando es estática
    gateway: Optional[str] = None    # puerta de enlace (opcional)


class DeployRequest(BaseModel):
    node_name: str
    template_id: int
    base_vm_name: str
    instance_count: int = Field(default=1, ge=1, le=20)
    start_vm_id: int = Field(ge=100, le=999999999)
    vm_cpu: int = Field(default=2, ge=1, le=128)
    vm_ram_mb: int = Field(default=2048, ge=512, le=524288)
    disk_size_gb: int = Field(default=20, ge=1, le=10240)
    admin_user: str = "administrator"
    admin_password: str
    # Interfaces de red con IP por-NIC. Si viene vacío, se usan los campos legacy de abajo.
    nics: List[Nic] = []
    # Legacy (despliegue single-NIC): se mantienen para compatibilidad.
    network_bridge: Optional[str] = None
    ip_mode: Optional[str] = None
    vm_ip_start: Optional[str] = None
    vm_gateway: Optional[str] = None
    vm_dns: List[str] = []
    vm_dns_domain: str = ""           # dominio de búsqueda DNS (search domain)
    ssh_keys: str = ""                # clave(s) SSH pública(s), una por línea
    ttl_hours: Optional[int] = None   # None / 0 → persistent ("Nunca")
    app_ids: List[str] = []           # herramientas a instalar vía guest-agent

    @field_validator("base_vm_name")
    @classmethod
    def validate_base_vm_name(cls, v: str) -> str:
        return _check_vm_name(v)

    @field_validator("app_ids")
    @classmethod
    def validate_app_ids(cls, v: List[str]) -> List[str]:
        invalid = [i for i in v if not tool_catalog.is_valid(i)]
        if invalid:
            raise ValueError(f"Herramienta(s) desconocida(s): {', '.join(invalid)}")
        return v

    @field_validator("ttl_hours")
    @classmethod
    def validate_ttl_hours(cls, v: Optional[int]) -> Optional[int]:
        if v is None or v == 0:
            return None
        if v < 1 or v > TTL_MAX_HOURS:
            raise ValueError(f"TTL fuera de rango: debe estar entre 1 y {TTL_MAX_HOURS} horas")
        return v


class UpdateResourceRequest(BaseModel):
    vm_cpu: int = Field(ge=1, le=128)
    vm_ram_mb: int = Field(ge=512, le=524288)
    disk_size_gb: int = Field(ge=1, le=10240)
    ssh_keys: str


class DestroyRequest(BaseModel):
    vm_name: str
    vmid: int
    node_name: str

    @field_validator("vm_name")
    @classmethod
    def validate_vm_name(cls, v: str) -> str:
        return _check_vm_name(v)


class ReprovisionRequest(BaseModel):
    vm_name: str
    vmid: int
    node_name: str
    admin_password: str

    @field_validator("vm_name")
    @classmethod
    def validate_vm_name(cls, v: str) -> str:
        return _check_vm_name(v)


class AdoptVMRequest(BaseModel):
    vm_name: str
    vmid: int
    node_name: str
    template_id: int = 0

    @field_validator("vm_name")
    @classmethod
    def validate_vm_name(cls, v: str) -> str:
        return _check_vm_name(v)


class CreateSnapshotRequest(BaseModel):
    snapname: str
    description: str = ""

    @field_validator("snapname")
    @classmethod
    def validate_snapname(cls, v: str) -> str:
        if not _SNAPNAME_RE.match(v):
            raise ValueError(
                "El nombre del snapshot solo puede contener letras, números, "
                "guiones y guiones bajos (máx. 40 caracteres, sin espacios)"
            )
        return v


# ── Internal helpers ──────────────────────────────────────────────────────────

def _increment_ip(ip_cidr: str, step: int) -> str:
    try:
        interface = ipaddress.IPv4Interface(ip_cidr)
        return f"{interface.ip + step}/{interface.network.prefixlen}"
    except ValueError:
        return ip_cidr


def _effective_nics(req: "DeployRequest") -> List[Nic]:
    """Devuelve las NICs a desplegar: la lista `nics` si viene; si no, una sola desde los campos legacy."""
    if req.nics:
        return req.nics
    is_dhcp = (req.ip_mode or "DHCP").upper() == "DHCP"
    return [Nic(
        bridge=req.network_bridge or "",
        ip_mode="dhcp" if is_dhcp else "static",
        ip=req.vm_ip_start,
        gateway=req.vm_gateway,
    )]


def _validate_nics(sess: UserSession, node: str, template_id: int, nics: List[Nic]) -> None:
    """Valida nodo/plantilla y, por NIC, que el bridge esté permitido y la IP estática sea correcta."""
    if not nics:
        raise HTTPException(status_code=400, detail="Se requiere al menos una interfaz de red.")
    # Reutiliza el gate existente para nodo + plantilla + primer bridge.
    assert_deploy_params_allowed(sess, node, template_id, nics[0].bridge)
    allowed = sess.client.get_networks(node)
    allowed_bridges = set(allowed.get("bridges", [])) | set(allowed.get("vnets", []))
    for n in nics:
        if not n.bridge:
            raise HTTPException(status_code=400, detail="Cada interfaz de red necesita un bridge/VNet.")
        if n.bridge not in allowed_bridges:
            raise HTTPException(
                status_code=403,
                detail=f"Acceso denegado: la red '{n.bridge}' no está disponible en '{node}'",
            )
        if (n.ip_mode or "dhcp").lower() == "static":
            err = validation.validate_static_cidr(n.ip or "")
            if err:
                raise HTTPException(status_code=400, detail=f"Interfaz '{n.bridge}': {err}")
            if n.gateway and validation.validate_ip(n.gateway):
                raise HTTPException(
                    status_code=400,
                    detail=f"Interfaz '{n.bridge}': puerta de enlace — {validation.validate_ip(n.gateway)}",
                )


def _nics_for_instance(nics: List[Nic], i: int) -> List[dict]:
    """NICs de la instancia `i`: cada NIC estática incrementa su IP; las DHCP se quedan en 'dhcp'."""
    out = []
    for n in nics:
        if (n.ip_mode or "dhcp").lower() == "static":
            out.append({"bridge": n.bridge, "ip": _increment_ip(n.ip, i), "gateway": n.gateway or ""})
        else:
            out.append({"bridge": n.bridge, "ip": "dhcp", "gateway": ""})
    return out


def _read_template_id_from_state(vm_name: str) -> int:
    """Returns the template_id stored in the VM's Terraform state, or 0 if absent."""
    try:
        state_path = os.path.join(
            tf_runner.tf_directory, "terraform.tfstate.d", vm_name, "terraform.tfstate"
        )
        if not os.path.exists(state_path):
            return 0
        with open(state_path) as f:
            state = json.load(f)
        vm_resources = [
            r for r in state.get("resources", [])
            if r.get("type") == "proxmox_virtual_environment_vm"
        ]
        if not vm_resources:
            return 0
        clone = vm_resources[0]["instances"][0]["attributes"].get("clone") or []
        return clone[0].get("vm_id", 0) if clone else 0
    except Exception:
        return 0


def _get_vm_current_resources(vm_name: str) -> tuple[int, int, float]:
    """Returns (cpu, ram_mb, disk_gb) for vm_name via the admin client. Falls back to zeros."""
    try:
        resources = pve_client.proxmox.cluster.resources.get(type="vm")
        vm = next((r for r in resources if r.get("name") == vm_name), None)
        if vm:
            return (
                vm.get("maxcpu", 0),
                int(vm.get("maxmem", 0) / (1024 * 1024)),
                round(vm.get("maxdisk", 0) / (1024 * 1024 * 1024), 2),
            )
    except Exception:
        pass
    return (0, 0, 0.0)


def _check_quota(
    pool_name: str,
    extra_cpu: int,
    extra_ram_mb: int,
    extra_disk_gb: float,
    client=None,
) -> None:
    quota = database.get_or_create_quota(pool_name)
    usage = (client or pve_client).get_pool_usage(pool_name)

    violations = []
    if usage["cpu"] + extra_cpu > quota["max_cpu"]:
        violations.append(
            f"CPU: {usage['cpu']} usadas + {extra_cpu} solicitadas = "
            f"{usage['cpu'] + extra_cpu} > límite {quota['max_cpu']} vCPU"
        )
    if usage["ram_mb"] + extra_ram_mb > quota["max_ram_mb"]:
        violations.append(
            f"RAM: {usage['ram_mb']} MB usados + {extra_ram_mb} MB solicitados = "
            f"{usage['ram_mb'] + extra_ram_mb} MB > límite {quota['max_ram_mb']} MB"
        )
    if round(usage["disk_gb"] + extra_disk_gb, 1) > quota["max_disk_gb"]:
        violations.append(
            f"Disco: {usage['disk_gb']:.1f} GB usados + {extra_disk_gb:.1f} GB solicitados = "
            f"{usage['disk_gb'] + extra_disk_gb:.1f} GB > límite {quota['max_disk_gb']} GB"
        )

    if violations:
        raise HTTPException(
            status_code=400,
            detail={"message": "Cuota del pool superada", "violations": violations},
        )


# ── App catalog ───────────────────────────────────────────────────────────────

@router.get("/tools")
def get_tool_catalog(sess: UserSession = Depends(get_current_session)):
    """Catálogo de herramientas instalables, agrupado por categoría (para el frontend)."""
    return tool_catalog.list_tools()


# ── VM lifecycle endpoints ────────────────────────────────────────────────────

@router.post("/deploy")
def deploy_infrastructure(
    request: DeployRequest,
    sess: UserSession = Depends(get_current_session),
):
    nics = _effective_nics(request)
    _validate_nics(sess, request.node_name, request.template_id, nics)

    pool_name = ProxmoxIDPClient._poolid_for(sess.userid)
    _check_quota(
        pool_name,
        extra_cpu=request.vm_cpu * request.instance_count,
        extra_ram_mb=request.vm_ram_mb * request.instance_count,
        extra_disk_gb=request.disk_size_gb * request.instance_count,
        client=sess.client,
    )

    userid = sess.userid

    def tf_job():
        poolid = pve_client.ensure_user_pool(userid)
        for i in range(request.instance_count):
            current_id = request.start_vm_id + i
            current_name = (
                f"{request.base_vm_name}-{i + 1}"
                if request.instance_count > 1
                else request.base_vm_name
            )
            inst_nics = _nics_for_instance(nics, i)

            config = VMConfig(
                vm_id=current_id,
                node_name=request.node_name,
                template_id=request.template_id,
                vm_name=current_name,
                network_bridge=inst_nics[0]["bridge"],
                network_nics=inst_nics,
                admin_user=request.admin_user,
                admin_password=request.admin_password,
                vm_dns=request.vm_dns,
                vm_dns_domain=request.vm_dns_domain,
                vm_ram=request.vm_ram_mb,
                vm_cpu=request.vm_cpu,
                disk_size=request.disk_size_gb,
                vm_pool=poolid,
                ssh_keys=request.ssh_keys,
            )
            tf_runner.deploy(config)

            # Comprobación de conectividad a internet: tarea de fondo SIN lock,
            # igual que la instalación de apps. Su resultado se persiste y se
            # muestra en el Live Task Stream (historial de despliegue).
            run_unlocked_task(
                task_type="Comprobación de red",
                target_name=current_name,
                func=lambda node=request.node_name, vmid=current_id, name=current_name:
                    connectivity.check_vm_internet(node, vmid, name),
                owner=userid,
            )

            # Register the ephemeral lifecycle only AFTER the VM is actually
            # created, and per-instance, so a mid-batch failure never leaves
            # phantom rows for VMs that don't exist.
            if request.ttl_hours:
                database.register_lifecycle(
                    vm_name=current_name,
                    vmid=current_id,
                    node_name=request.node_name,
                    owner=userid,
                    pool_name=poolid,
                    expires_at=time.time() + request.ttl_hours * 3600,
                )

            # Instalación de herramientas vía guest-agent: tarea de fondo SIN lock
            # (el submit es instantáneo; la instalación corre fuera de la cola TF).
            if request.app_ids:
                run_unlocked_task(
                    task_type="Instalación de apps",
                    target_name=current_name,
                    func=lambda node=request.node_name, vmid=current_id, name=current_name:
                        app_installer.install_apps(node, vmid, name, request.app_ids),
                    owner=userid,
                )

    task_id = create_and_queue_task(
        task_type="Despliegue",
        target_name=request.base_vm_name + (
            f" (x{request.instance_count})" if request.instance_count > 1 else ""
        ),
        func=tf_job,
        owner=sess.userid,
    )
    return {"message": "Despliegue añadido a la cola", "task_id": task_id}


@router.put("/deploy/{vm_name}")
def update_infrastructure(
    vm_name: str,
    request: UpdateResourceRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_name(sess, vm_name)

    pool_name = ProxmoxIDPClient._poolid_for(sess.userid)
    old_cpu, old_ram_mb, old_disk_gb = _get_vm_current_resources(vm_name)
    _check_quota(
        pool_name,
        extra_cpu=request.vm_cpu - old_cpu,
        extra_ram_mb=request.vm_ram_mb - old_ram_mb,
        extra_disk_gb=request.disk_size_gb - old_disk_gb,
        client=sess.client,
    )

    def tf_job():
        tf_runner.update(vm_name, request.model_dump())

    task_id = create_and_queue_task("Mutación de Recursos", vm_name, tf_job, owner=sess.userid)
    return {"message": "Mutación añadida a la cola", "task_id": task_id}


@router.post("/destroy")
def destroy_infrastructure(
    request: DestroyRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, request.vmid)

    def tf_job():
        tf_runner.destroy(request.vm_name, request.vmid, request.node_name)
        # Drop any lifecycle row so the reaper never chases a destroyed VM.
        database.delete_lifecycle(request.vm_name)

    task_id = create_and_queue_task("Destrucción", request.vm_name, tf_job, owner=sess.userid)
    return {"message": "Destrucción añadida a la cola", "task_id": task_id}


@router.post("/reprovision")
def reprovision_infrastructure(
    request: ReprovisionRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, request.vmid)

    if _read_template_id_from_state(request.vm_name) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Esta VM no puede reprovisionarse: no tiene plantilla de origen conocida. "
                "Fue adoptada sin seleccionar plantilla — usa Snapshots para restaurar un punto anterior."
            ),
        )

    def tf_job():
        try:
            pve_client.set_vm_power_state(request.node_name, request.vmid, "stop")
        except Exception:
            pass
        tf_runner.reprovision(request.vm_name, request.admin_password)

    task_id = create_and_queue_task(
        "Nuke & Pave (Reprovisionar)", request.vm_name, tf_job, owner=sess.userid
    )
    return {"message": "Reprovisionamiento añadido a la cola", "task_id": task_id}


@router.post("/adopt")
def adopt_vm(
    request: AdoptVMRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, request.vmid)

    userid = sess.userid
    _vm_name, _vmid, _node, _template_id = (
        request.vm_name, request.vmid, request.node_name, request.template_id
    )

    def adopt_job():
        tf_runner.adopt(_vm_name, _node, _vmid, _template_id)
        pve_client.add_managed_tag(_node, _vmid)

    task_id = create_and_queue_task("Adoptar VM", _vm_name, adopt_job, owner=userid)
    return {"message": "Adopción encolada", "task_id": task_id}


# ── Snapshot endpoints ────────────────────────────────────────────────────────

@router.get("/vms/{node}/{vmid}/snapshots")
def list_snapshots(
    node: str,
    vmid: int,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, vmid)
    try:
        return sess.client.get_snapshots(node, vmid)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/vms/{node}/{vmid}/snapshots")
def create_snapshot(
    node: str,
    vmid: int,
    body: CreateSnapshotRequest,
    sess: UserSession = Depends(get_current_session),
):
    assert_vm_accessible_by_vmid(sess, vmid)
    _node, _vmid, _snapname, _desc = node, vmid, body.snapname, body.description

    def job():
        pve_client.create_snapshot(_node, _vmid, _snapname, _desc)

    task_id = create_and_queue_task(
        "Crear Snapshot", f"VM {vmid} → {body.snapname}", job, owner=sess.userid
    )
    return {"message": "Snapshot encolado", "task_id": task_id}


@router.post("/vms/{node}/{vmid}/snapshots/{snapname}/rollback")
def rollback_snapshot(
    node: str,
    vmid: int,
    snapname: str,
    sess: UserSession = Depends(get_current_session),
):
    _check_snapname_param(snapname)
    assert_vm_accessible_by_vmid(sess, vmid)
    if snapname == "current":
        raise HTTPException(
            status_code=400, detail="No se puede restaurar el pseudo-snapshot 'current'"
        )
    _node, _vmid, _snapname = node, vmid, snapname

    def job():
        pve_client.rollback_snapshot(_node, _vmid, _snapname)

    task_id = create_and_queue_task(
        "Restaurar Snapshot", f"VM {vmid} → {snapname}", job, owner=sess.userid
    )
    return {"message": "Restauración encolada", "task_id": task_id}


@router.delete("/vms/{node}/{vmid}/snapshots/{snapname}")
def delete_snapshot(
    node: str,
    vmid: int,
    snapname: str,
    sess: UserSession = Depends(get_current_session),
):
    _check_snapname_param(snapname)
    assert_vm_accessible_by_vmid(sess, vmid)
    if snapname == "current":
        raise HTTPException(
            status_code=400, detail="No se puede borrar el pseudo-snapshot 'current'"
        )
    _node, _vmid, _snapname = node, vmid, snapname

    def job():
        pve_client.delete_snapshot(_node, _vmid, _snapname)

    task_id = create_and_queue_task(
        "Borrar Snapshot", f"VM {vmid} → {snapname}", job, owner=sess.userid
    )
    return {"message": "Borrado de snapshot encolado", "task_id": task_id}
