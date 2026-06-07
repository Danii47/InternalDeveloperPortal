"""
Motor de orquestación de Blueprints.

Ejecuta los `steps` de una receta de forma SECUENCIAL reutilizando los servicios existentes:
cada paso se encola en la cola serializada (`create_and_queue_task`) y se ESPERA su resultado
(`wait_for_task`) antes de pasar al siguiente. El propio run corre como tarea "padre" en el pool
sin-lock (`run_unlocked_task`), de modo que esperar entre pasos NO retiene `deployment_lock`.

Errores: por defecto HALT (detener y reportar lo creado). Con `rollback`, compensación inversa
(patrón saga): se deshacen los pasos completados en orden inverso con sus compensadores.

Seguridad: las `action` provienen de un registro CERRADO; los params se resuelven con
`blueprint_loader.resolve` (sin eval) y cada handler re-valida/sanea su entrada.
"""
import ipaddress
import logging
import re
import secrets
import time

import database
from core.deps import pve_client, tf_runner
from services import actions, app_installer, blueprint_loader, validation
from services.task_manager import (
    TaskStatus,
    create_and_queue_task,
    run_inline_task,
    run_unlocked_task,
    wait_for_task,
)
from services.terraform_runner import VMConfig

logger = logging.getLogger("orchestrator")

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,62}$")

# Límite de VMs por paso deploy_vms (evita agotar el clúster por un valor accidental/abusivo).
MAX_VMS_PER_STEP = 50

# Acciones que tocan Terraform → se encolan en la cola serializada (deployment_lock).
# El resto (API pura: pools, SDN, guest-agent, power, snapshot) corren sin lock.
_TF_ACTIONS = {"deploy_vms"}


def _split_list(val) -> list:
    """Normaliza un valor a lista de strings (acepta lista o cadena separada por comas)."""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if val is None or str(val).strip() == "":
        return []
    return [s.strip() for s in str(val).split(",") if s.strip()]


def _increment_ip(ip_cidr: str, step: int) -> str:
    try:
        iface = ipaddress.IPv4Interface(ip_cidr)
        return f"{iface.ip + step}/{iface.network.prefixlen}"
    except ValueError:
        return ip_cidr


def _san(value, maxlen: int = None) -> str:
    """Sanitiza a [a-z0-9-] (ids de pool/vnet/zone). Trunca si se indica maxlen."""
    out = re.sub(r"[^a-zA-Z0-9-]", "-", str(value)).strip("-").lower()
    return out[:maxlen] if maxlen else out


def compute_derived(ctx: dict) -> dict:
    """
    Variables derivadas que inyecta la plataforma (documentadas en blueprint_loader.DERIVED_KEYS).
    `vnet_id`: nombre corto/estable para la VNet (Proxmox limita a 8 chars alfanuméricos).
    """
    env = ctx.get("env_name")
    if env:
        ctx["vnet_id"] = ("v" + re.sub(r"[^a-z0-9]", "", str(env).lower()))[:8]
    return ctx


# ── Handlers de acción ───────────────────────────────────────────────────────────
# Cada handler es `factory(params, owner) -> run()`; `run` ejecuta y devuelve una ref
# (para el rollback). Reutilizan los servicios existentes y re-sanean su entrada.

def _int_or(val, fallback: int) -> int:
    """Convierte a int si hay valor utilizable; si no, devuelve el fallback."""
    if val is None or str(val).strip() in ("", "None"):
        return fallback
    return int(val)


def _opt(params: dict, key: str):
    """Devuelve el valor de un param opcional ya 'trim'eado, o None si está vacío."""
    v = params.get(key)
    v = str(v).strip() if v is not None else ""
    return v or None


def _require_valid(kind: str, value: str) -> None:
    err = validation.validate_name(kind, value)
    if err:
        raise ValueError(err)


def _h_create_group(params: dict, owner: str):
    gid = str(params["groupid"]).strip()
    _require_valid("group", gid)
    comment = str(params.get("comment", ""))

    def run():
        pve_client.create_group(gid, comment=comment)
        return {"groupid": gid}

    return run


def _comp_create_group(ref: dict):
    pve_client.delete_group(ref["groupid"])


def _h_create_pool(params: dict, owner: str):
    poolid = _san(params["poolid"])
    _require_valid("pool", poolid)
    comment = str(params.get("comment", ""))
    group = _opt(params, "group")
    q_cpu, q_ram, q_disk = params.get("quota_cpu"), params.get("quota_ram_mb"), params.get("quota_disk_gb")

    def run():
        pve_client.create_pool(poolid, comment=comment, groups=group)
        # Si se indicó alguna cuota, fija las tres (las no indicadas conservan el valor actual).
        if any(v is not None and str(v).strip() not in ("", "None") for v in (q_cpu, q_ram, q_disk)):
            cur = database.get_or_create_quota(poolid)
            database.update_quota(
                poolid,
                _int_or(q_cpu, cur["max_cpu"]),
                _int_or(q_ram, cur["max_ram_mb"]),
                _int_or(q_disk, cur["max_disk_gb"]),
            )
        return {"poolid": poolid, "group": group}

    return run


def _comp_create_pool(ref: dict):
    pve_client.delete_pool(ref["poolid"])


def _h_create_vnet(params: dict, owner: str):
    vnet = str(params["vnet"]).strip()
    _require_valid("vnet", vnet)   # ≤8, alfanumérico; NO se trunca (evita desajuste con el bridge)
    zone = (str(params.get("zone") or "idpzone")).strip()
    _require_valid("zone", zone)
    subnet = _opt(params, "subnet")
    if subnet:
        err = validation.validate_cidr(subnet)
        if err:
            raise ValueError(err)
    group = _opt(params, "group")
    alias = _opt(params, "alias")  # nombre descriptivo de la VNet (opcional)
    tag = _opt(params, "tag")  # VLAN id / VNI; en zonas vlan/vxlan/qinq se autoasigna si falta
    if tag is not None and not tag.isdigit():
        raise ValueError(f"El tag de la VNet debe ser un número (VLAN id / VNI), no «{tag}».")

    def run():
        ref = pve_client.create_vnet(vnet, zone=zone, subnet=subnet, tag=tag, alias=alias)
        if group:
            pve_client.grant_acl(f"/sdn/zones/{zone}/{vnet}", "PVESDNUser", groups=group)
        ref["group"] = group
        return ref

    return run


def _comp_create_vnet(ref: dict):
    pve_client.delete_vnet(ref["vnet"])


def _h_deploy_vms(params: dict, owner: str):
    count = int(params.get("count", 1))
    if count < 1 or count > MAX_VMS_PER_STEP:
        raise ValueError(f"count fuera de rango (1-{MAX_VMS_PER_STEP})")
    base = str(params["base_name"])
    if not _NAME_RE.match(base):
        raise ValueError(f"base_name inválido: {base!r}")
    node = str(params["node"])
    template_id = int(params["template_id"])
    pool = _san(params["pool"]) if params.get("pool") else ""
    bridges = _split_list(params.get("bridges"))
    if not bridges:
        raise ValueError("deploy_vms: se requiere al menos una red en 'bridges'")
    cpu = int(params.get("cpu", 2))
    ram = int(params.get("ram_mb", 2048))
    disk = int(params.get("disk_gb", 20))
    admin_user = str(params.get("admin_user") or "root")
    admin_password = str(params.get("admin_password") or "") or secrets.token_urlsafe(18)
    is_static = str(params.get("ip_mode", "dhcp")).lower() == "static"
    ip_start = params.get("ip_start")
    gateway = str(params.get("gateway") or "")
    dns = _split_list(params.get("dns"))
    dns_domain = str(params.get("dns_domain") or "")
    ssh_keys = str(params.get("ssh_keys") or "")
    _ttl = params.get("ttl_hours")
    ttl_hours = int(_ttl) if str(_ttl).strip() not in ("", "None", "0") else None
    app_ids = _split_list(params.get("app_ids"))

    # Validación autoritativa (mismas reglas que el pre-flight; defensa en runtime).
    if is_static:
        if not ip_start or not str(ip_start).strip():
            raise ValueError("deploy_vms: con IP estática hay que indicar 'ip_start' en CIDR (ej. 10.0.0.10/24).")
        err = validation.validate_static_cidr(str(ip_start))
        if err:
            raise ValueError(f"deploy_vms: {err}")
        if gateway and validation.validate_ip(gateway):
            raise ValueError(f"deploy_vms: puerta de enlace — {validation.validate_ip(gateway)}")
    for d in dns:
        if validation.validate_ip(d):
            raise ValueError(f"deploy_vms: DNS — {validation.validate_ip(d)}")

    def run():
        base_id = pve_client.get_next_vmid()
        if not isinstance(base_id, int) or base_id < 100:
            raise RuntimeError("No se pudo obtener un VMID válido del clúster (¿API/permisos?).")
        created = []
        try:
            for i in range(count):
                vmid = base_id + i
                name = f"{base}-{i + 1}"
                vm_ip = _increment_ip(ip_start, i) if (is_static and ip_start) else "dhcp"
                cfg = VMConfig(
                    vm_id=vmid, node_name=node, template_id=template_id, vm_name=name,
                    network_bridge=bridges[0], network_bridges=bridges,
                    admin_user=admin_user, admin_password=admin_password,
                    vm_ip=vm_ip, vm_gateway=(gateway if is_static else ""),
                    vm_dns=dns, vm_dns_domain=dns_domain, ssh_keys=ssh_keys,
                    vm_ram=ram, vm_cpu=cpu, disk_size=disk, vm_pool=pool,
                )
                tf_runner.deploy(cfg)
                created.append({"vm_name": name, "vmid": vmid, "node": node})
                if ttl_hours:
                    database.register_lifecycle(name, vmid, node, owner, pool,
                                                time.time() + ttl_hours * 3600)
                if app_ids:
                    # Instalación asíncrona sin lock (igual que el despliegue normal).
                    run_unlocked_task(
                        "Instalación de apps", name,
                        (lambda n=node, v=vmid, nm=name: app_installer.install_apps(n, v, nm, app_ids)),
                        owner,
                    )
        except Exception:
            for vm in reversed(created):   # atomicidad: deshace lo creado en este paso
                try:
                    tf_runner.destroy(vm["vm_name"], vm["vmid"], vm["node"])
                    database.delete_lifecycle(vm["vm_name"])
                except Exception:
                    pass
            raise
        return {"vms": created}

    return run


def _comp_deploy_vms(ref: dict):
    errs = []
    for vm in ref.get("vms", []):
        try:
            tf_runner.destroy(vm["vm_name"], vm["vmid"], vm["node"])
            database.delete_lifecycle(vm["vm_name"])
        except Exception as exc:
            errs.append(f"{vm['vm_name']}: {exc}")
    if errs:
        raise RuntimeError("; ".join(errs))


def _resolve_target(name: str) -> dict:
    vm = pve_client.get_vm_by_name(name)
    if not vm:
        raise RuntimeError(f"VM '{name}' no encontrada en el clúster")
    return vm


def _h_install_apps(params: dict, owner: str):
    target = str(params["target_vm"])
    app_ids = _split_list(params.get("app_ids"))

    def run():
        vm = _resolve_target(target)
        app_installer.install_apps(vm["node"], vm["vmid"], target, app_ids)
        return {"target": target}

    return run


def _h_power_action(params: dict, owner: str):
    target = str(params["target_vm"])
    action = str(params.get("action", "start")).lower()
    if action not in ("start", "stop"):
        raise ValueError("power_action: 'action' debe ser 'start' o 'stop'")

    def run():
        vm = _resolve_target(target)
        pve_client.set_vm_power_state(vm["node"], vm["vmid"], action)
        return {"target": target, "action": action}

    return run


def _h_create_snapshot(params: dict, owner: str):
    target = str(params["target_vm"])
    snapname = str(params["snapname"])
    description = str(params.get("description", ""))

    def run():
        vm = _resolve_target(target)
        pve_client.create_snapshot(vm["node"], vm["vmid"], snapname, description)
        return {"target": target, "snapname": snapname, "node": vm["node"], "vmid": vm["vmid"]}

    return run


def _comp_create_snapshot(ref: dict):
    pve_client.delete_snapshot(ref["node"], ref["vmid"], ref["snapname"])


STEP_REGISTRY = {
    "create_group": _h_create_group,
    "create_pool": _h_create_pool,
    "create_vnet": _h_create_vnet,
    "deploy_vms": _h_deploy_vms,
    "install_apps": _h_install_apps,
    "power_action": _h_power_action,
    "create_snapshot": _h_create_snapshot,
}
COMPENSATORS = {
    "create_group": _comp_create_group,
    "create_pool": _comp_create_pool,
    "create_vnet": _comp_create_vnet,
    "deploy_vms": _comp_deploy_vms,
    "create_snapshot": _comp_create_snapshot,
}

# El registro debe cubrir EXACTAMENTE el manifiesto de acciones (evita desincronización).
assert set(STEP_REGISTRY) == actions.ALLOWED_ACTIONS, (
    f"STEP_REGISTRY no cubre ACTION_SCHEMA: {set(STEP_REGISTRY) ^ actions.ALLOWED_ACTIONS}"
)


# ── Bucle de ejecución ───────────────────────────────────────────────────────────

def run_blueprint(run_id: str, bp: dict, ctx: dict, owner: str, policy: str) -> None:
    steps = bp["steps"]
    states = [
        {
            "id": s["id"], "action": s["action"],
            "name": blueprint_loader.resolve(s.get("name", s["id"]), ctx),
            "status": "pending", "error": None, "ref": None,
        }
        for s in steps
    ]
    database.update_run(run_id, status="running", steps=states)
    executed = []  # [(action, ref)] de pasos completados, para rollback

    for i, s in enumerate(steps):
        try:
            params = blueprint_loader.resolve(s.get("params", {}), ctx)
            step_func = STEP_REGISTRY[s["action"]](params, owner)   # re-valida/sanea
        except Exception as exc:
            states[i]["status"] = "failed"
            states[i]["error"] = f"preparación: {exc}"
            database.update_run(run_id, steps=states)
            return _handle_failure(run_id, policy, executed, states, owner)

        states[i]["status"] = "running"
        database.update_run(run_id, steps=states)

        target = ctx.get("env_name", bp["id"])
        if s["action"] in _TF_ACTIONS:
            # Terraform → cola serializada (deployment_lock); el padre espera sin el lock.
            tid = create_and_queue_task(states[i]["name"], target, step_func, owner)
            status, error, ref = wait_for_task(tid)
        else:
            # API pura → inline en el hilo del padre (sin pool → sin contención/deadlock).
            status, error, ref = run_inline_task(states[i]["name"], target, step_func, owner)

        if status == TaskStatus.COMPLETED:
            states[i]["status"] = "completed"
            states[i]["ref"] = ref
            executed.append((s["action"], ref))
            database.update_run(run_id, steps=states)
        else:
            states[i]["status"] = "failed"
            states[i]["error"] = error
            database.update_run(run_id, steps=states)
            return _handle_failure(run_id, policy, executed, states, owner)

    database.update_run(run_id, status="completed", steps=states)
    logger.info("Blueprint run %s completado.", run_id)


def _handle_failure(run_id: str, policy: str, executed: list, states: list, owner: str) -> None:
    if policy != "rollback":
        database.update_run(run_id, status="failed")
        logger.warning("Run %s detenido (HALT). Recursos creados: %s",
                       run_id, [a for a, _ in executed])
        return

    partial = False
    for action, ref in reversed(executed):
        comp = COMPENSATORS.get(action)
        if not comp or not ref:
            continue
        tid = create_and_queue_task(
            f"Rollback: {action}", run_id, (lambda c=comp, r=ref: c(r)), owner
        )
        st, err, _ = wait_for_task(tid)
        if st != TaskStatus.COMPLETED:
            partial = True
            logger.error("Rollback de %s falló: %s", action, err)

    database.update_run(run_id, status="rollback_partial" if partial else "rolled_back")
    logger.info("Run %s rollback %s.", run_id, "parcial" if partial else "completo")


def _guarded_run(run_id: str, bp: dict, ctx: dict, owner: str, policy: str) -> None:
    """Envuelve run_blueprint: ante un error inesperado marca el run 'failed' (no 'running')."""
    try:
        run_blueprint(run_id, bp, ctx, owner, policy)
    except Exception:
        logger.exception("Run %s abortado por error inesperado", run_id)
        try:
            database.update_run(run_id, status="failed")
        except Exception:
            pass
        raise


def launch(run_id: str, bp: dict, ctx: dict, owner: str, policy: str) -> str:
    """Lanza el run como tarea padre sin-lock. Devuelve el task_id del padre."""
    return run_unlocked_task(
        f"Blueprint: {bp['name']}",
        ctx.get("env_name", bp["id"]),
        lambda: _guarded_run(run_id, bp, ctx, owner, policy),
        owner,
    )
