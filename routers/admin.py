import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
from core.deps import pve_client
from core.security import UserSession, get_current_session, require_admin
from services import validation
from services.proxmox_client import ProxmoxIDPClient

router = APIRouter(prefix="/api", tags=["admin", "quotas"])


class UpdateQuotaRequest(BaseModel):
    max_cpu: int
    max_ram_mb: int
    max_disk_gb: int


class AddMemberRequest(BaseModel):
    userid: str


class CreatePoolRequest(BaseModel):
    poolid: str
    comment: str = ""
    group: Optional[str] = None
    quota_cpu: Optional[int] = None
    quota_ram_gb: Optional[int] = None
    quota_disk_gb: Optional[int] = None


class CreateGroupRequest(BaseModel):
    groupid: str
    comment: str = ""


class CreateVnetRequest(BaseModel):
    vnet: str
    zone: str = "idpzone"
    alias: Optional[str] = None
    tag: Optional[str] = None
    subnet: Optional[str] = None
    group: Optional[str] = None


def _san(value: str, maxlen: int = None) -> str:
    """Sanitiza a [a-z0-9-] (id de pool). Replica services.orchestrator._san."""
    out = re.sub(r"[^a-zA-Z0-9-]", "-", str(value)).strip("-").lower()
    return out[:maxlen] if maxlen else out


def _build_quota_entry(pool_name: str, client) -> dict:
    return {
        "pool_name": pool_name,
        "quota": database.get_or_create_quota(pool_name),
        "usage": client.get_pool_usage(pool_name),
    }


def _friendly_pve_error(exc: Exception) -> str:
    """Traduce un error de Proxmox/proxmoxer a un mensaje legible para el usuario."""
    raw = str(exc).strip()
    msg = raw.split(": ", 1)[1] if ": " in raw else raw   # quita prefijo "NNN Status:"
    low = msg.lower()
    if "not empty" in low:
        return "el recurso aún tiene elementos dentro; vacíalo en Proxmox antes de borrarlo."
    if "does not exist" in low or "no such" in low or "unknown" in low:
        return "no existe en Proxmox (puede que ya se haya eliminado)."
    if "permission" in low or raw.startswith("403"):
        return "permisos insuficientes en Proxmox para esta operación."
    return msg or "error desconocido de Proxmox."


@router.get("/quotas")
def get_user_quotas(sess: UserSession = Depends(get_current_session)):
    pool_name = ProxmoxIDPClient._poolid_for(sess.userid)
    return [_build_quota_entry(pool_name, sess.client)]


@router.put("/admin/quotas/{pool_name}")
def set_pool_quota(
    pool_name: str,
    body: UpdateQuotaRequest,
    sess: UserSession = Depends(require_admin),
):
    if body.max_cpu <= 0 or body.max_ram_mb <= 0 or body.max_disk_gb <= 0:
        raise HTTPException(status_code=400, detail="Los límites de cuota deben ser positivos")
    updated = database.update_quota(pool_name, body.max_cpu, body.max_ram_mb, body.max_disk_gb)
    return {"message": f"Cuota de '{pool_name}' actualizada", "quota": updated}


# ── Gestión de Pools y Grupos (admin) ─────────────────────────────────────────

@router.post("/admin/pools", status_code=201)
def create_pool(body: CreatePoolRequest, sess: UserSession = Depends(require_admin)):
    """Crea un Resource Pool nuevo (idempotente), igual que la acción `create_pool` de Blueprints."""
    poolid = _san(body.poolid)
    err = validation.validate_name("pool", poolid)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if sess.client.pool_exists(poolid):
        raise HTTPException(status_code=409, detail=f"El pool '{poolid}' ya existe.")

    for label, v in (("quota_cpu", body.quota_cpu), ("quota_ram_gb", body.quota_ram_gb),
                     ("quota_disk_gb", body.quota_disk_gb)):
        if v is not None and v <= 0:
            raise HTTPException(status_code=400, detail=f"'{label}' debe ser un valor positivo.")

    group = (body.group or "").strip() or None
    try:
        sess.client.create_pool(poolid, comment=body.comment or "", groups=group)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo crear el pool: {_friendly_pve_error(exc)}")

    if any(v is not None for v in (body.quota_cpu, body.quota_ram_gb, body.quota_disk_gb)):
        cur = database.get_or_create_quota(poolid)
        max_ram_mb = int(body.quota_ram_gb) * 1024 if body.quota_ram_gb is not None else cur["max_ram_mb"]
        database.update_quota(
            poolid,
            body.quota_cpu if body.quota_cpu is not None else cur["max_cpu"],
            max_ram_mb,
            body.quota_disk_gb if body.quota_disk_gb is not None else cur["max_disk_gb"],
        )
    return {"message": f"Pool '{poolid}' creado correctamente.", "poolid": poolid}


@router.get("/admin/pools")
def list_pools(sess: UserSession = Depends(require_admin)):
    """Pools del clúster con su cuota, uso y nº de recursos (para la pestaña Pools y Grupos)."""
    out = []
    for p in sess.client.get_all_pools():
        entry = _build_quota_entry(p, sess.client)
        entry["member_count"] = len(sess.client.get_pool_members(p))
        out.append(entry)
    return out


@router.delete("/admin/pools/{pool_name}")
def delete_pool(pool_name: str, sess: UserSession = Depends(require_admin)):
    """
    Elimina un pool. Bloquea (sin tocar nada) si todavía contiene recursos. Tras borrarlo, limpia
    las ACL de grupo colgantes sobre /pool/<id> y la fila de cuota local.
    """
    members = sess.client.get_pool_members(pool_name)
    if members:
        raise HTTPException(
            status_code=400,
            detail=(f"El pool '{pool_name}' tiene {len(members)} recurso(s) dentro. "
                    "Muévelos a otro pool o elimínalos antes de borrar el pool."),
        )
    try:
        sess.client.delete_pool(pool_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo borrar el pool: {_friendly_pve_error(exc)}")

    for a in sess.client.get_acls():
        if a.get("type") == "group" and a.get("path") == f"/pool/{pool_name}" and a.get("ugid"):
            try:
                sess.client.remove_acl(a["path"], a.get("roleid"), groups=a["ugid"])
            except Exception:
                pass
    database.delete_quota(pool_name)
    return {"message": f"Pool '{pool_name}' eliminado correctamente."}


@router.post("/admin/groups", status_code=201)
def create_group(body: CreateGroupRequest, sess: UserSession = Depends(require_admin)):
    """Crea un grupo de Proxmox nuevo (idempotente), igual que la acción `create_group` de Blueprints."""
    gid = body.groupid.strip()
    err = validation.validate_name("group", gid)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if sess.client.group_exists(gid):
        raise HTTPException(status_code=409, detail=f"El grupo '{gid}' ya existe.")
    try:
        sess.client.create_group(gid, comment=body.comment or "")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo crear el grupo: {_friendly_pve_error(exc)}")
    return {"message": f"Grupo '{gid}' creado correctamente.", "groupid": gid}


@router.get("/admin/groups")
def list_groups(sess: UserSession = Depends(require_admin)):
    """
    Grupos del clúster con sus miembros y los pool(es)/VNets a los que tienen acceso (vía ACL).
    Marca `exists=False` en pools cuya ACL quedó colgante (el pool ya no existe).
    """
    groups = sess.client.get_groups()
    existing_pools = set(sess.client.get_all_pools())
    by_group: dict = {}
    for a in sess.client.get_acls():
        if a.get("type") == "group" and a.get("ugid"):
            by_group.setdefault(a["ugid"], []).append(a)

    out = []
    for g in groups:
        gid = g.get("groupid")
        members = [m.strip() for m in str(g.get("users", "")).split(",") if m.strip()]
        pools, vnets = [], []
        for a in by_group.get(gid, []):
            path = a.get("path", "")
            if path.startswith("/pool/"):
                pid = path.split("/", 2)[2]
                pools.append({"poolid": pid, "role": a.get("roleid"), "exists": pid in existing_pools})
            elif path.startswith("/sdn/zones/"):
                parts = path.split("/")  # ['', 'sdn', 'zones', '<zona>', '<vnet>']
                if len(parts) >= 5:
                    vnets.append({"zone": parts[3], "vnet": parts[4], "role": a.get("roleid")})
        out.append({"groupid": gid, "comment": g.get("comment", ""),
                    "members": members, "pools": pools, "vnets": vnets})
    return out


@router.delete("/admin/groups/{groupid}")
def delete_group(groupid: str, sess: UserSession = Depends(require_admin)):
    """Elimina un grupo. Limpia antes sus ACL (para no dejar entradas colgantes)."""
    for a in sess.client.get_acls():
        if a.get("type") == "group" and a.get("ugid") == groupid and a.get("path"):
            try:
                sess.client.remove_acl(a["path"], a.get("roleid"), groups=groupid)
            except Exception:
                pass
    try:
        sess.client.delete_group(groupid)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo borrar el grupo: {_friendly_pve_error(exc)}")
    return {"message": f"Grupo '{groupid}' eliminado correctamente."}


@router.get("/admin/users")
def list_users(sess: UserSession = Depends(require_admin)):
    """Usuarios del clúster (para el selector de 'añadir a grupo')."""
    return [{"userid": u.get("userid")} for u in sess.client.get_users() if u.get("userid")]


@router.post("/admin/groups/{groupid}/members")
def add_group_member(groupid: str, body: AddMemberRequest, sess: UserSession = Depends(require_admin)):
    """Añade un usuario a un grupo."""
    try:
        sess.client.add_user_to_group(body.userid, groupid)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo añadir el usuario: {_friendly_pve_error(exc)}")
    return {"message": f"'{body.userid}' añadido al grupo '{groupid}'."}


@router.delete("/admin/groups/{groupid}/members/{userid}")
def remove_group_member(groupid: str, userid: str, sess: UserSession = Depends(require_admin)):
    """Quita un usuario de un grupo."""
    try:
        sess.client.remove_user_from_group(userid, groupid)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo quitar el usuario: {_friendly_pve_error(exc)}")
    return {"message": f"'{userid}' quitado del grupo '{groupid}'."}


# ── Gestión de VNets SDN (admin) ──────────────────────────────────────────────
# Las operaciones SDN (zonas/VNets) usan el cliente GLOBAL (token admin), igual que
# el orquestador de Blueprints (services/orchestrator.py) y /admin/blueprints/sdn-zones:
# el ticket impersonado del usuario no tiene por qué tener privilegios sobre /sdn.

@router.get("/admin/sdn-zones")
def list_sdn_zones(sess: UserSession = Depends(require_admin)):
    """Zonas SDN existentes (para el desplegable de zona al crear una VNet)."""
    return pve_client.get_sdn_zones()


@router.get("/admin/vnets")
def list_vnets(sess: UserSession = Depends(require_admin)):
    """VNets SDN del clúster con su configuración completa (zona, alias, tag, subredes, grupos con acceso)."""
    try:
        vnets = pve_client.proxmox.cluster.sdn.vnets.get() or []
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudieron obtener las VNets: {_friendly_pve_error(exc)}")

    groups_by_vnet: dict = {}
    for a in pve_client.get_acls():
        path = a.get("path", "")
        if a.get("type") == "group" and a.get("ugid") and path.startswith("/sdn/zones/"):
            parts = path.split("/")  # ['', 'sdn', 'zones', '<zona>', '<vnet>']
            if len(parts) >= 5:
                groups_by_vnet.setdefault((parts[3], parts[4]), []).append(
                    {"groupid": a["ugid"], "role": a.get("roleid")}
                )

    out = []
    for v in vnets:
        vnet_id, zone = v.get("vnet"), v.get("zone")
        try:
            subnets = pve_client.proxmox.cluster.sdn.vnets(vnet_id).subnets.get() or []
        except Exception:
            subnets = []
        out.append({
            "vnet": vnet_id,
            "zone": zone,
            "alias": v.get("alias") or "",
            "tag": v.get("tag"),
            "subnets": [
                {"cidr": s.get("cidr") or s.get("subnet"), "gateway": s.get("gateway")}
                for s in subnets
            ],
            "groups": groups_by_vnet.get((zone, vnet_id), []),
        })
    return out


@router.post("/admin/vnets", status_code=201)
def create_vnet(body: CreateVnetRequest, sess: UserSession = Depends(require_admin)):
    """Crea una VNet SDN nueva (idempotente), igual que la acción `create_vnet` de Blueprints."""
    vnet = body.vnet.strip()
    err = validation.validate_name("vnet", vnet)
    if err:
        raise HTTPException(status_code=400, detail=err)
    zone = (body.zone or "idpzone").strip()
    err = validation.validate_name("zone", zone)
    if err:
        raise HTTPException(status_code=400, detail=err)

    existing = {v["vnet"] for v in pve_client.proxmox.cluster.sdn.vnets.get()}
    if vnet in existing:
        raise HTTPException(status_code=409, detail=f"La VNet '{vnet}' ya existe.")

    subnet = (body.subnet or "").strip() or None
    if subnet:
        err = validation.validate_cidr(subnet)
        if err:
            raise HTTPException(status_code=400, detail=err)

    tag = (body.tag or "").strip() or None
    if tag is not None and not tag.isdigit():
        raise HTTPException(status_code=400,
                             detail=f"El tag de la VNet debe ser un número (VLAN id / VNI), no «{tag}».")

    alias = (body.alias or "").strip() or None
    group = (body.group or "").strip() or None

    try:
        ref = pve_client.create_vnet(vnet, zone=zone, subnet=subnet, tag=tag, alias=alias)
        if group:
            pve_client.grant_acl(f"/sdn/zones/{zone}/{vnet}", "PVESDNUser", groups=group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo crear la VNet: {_friendly_pve_error(exc)}")

    ref["group"] = group
    return {"message": f"VNet '{vnet}' creada correctamente.", "vnet": ref}


@router.delete("/admin/vnets/{vnet}")
def delete_vnet(vnet: str, sess: UserSession = Depends(require_admin)):
    """Elimina una VNet SDN y limpia las ACL de grupo colgantes que apuntaban a ella."""
    try:
        pve_client.delete_vnet(vnet)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo borrar la VNet: {_friendly_pve_error(exc)}")

    for a in pve_client.get_acls():
        path = a.get("path", "")
        if a.get("type") == "group" and a.get("ugid") and path.startswith("/sdn/zones/") and path.endswith(f"/{vnet}"):
            try:
                pve_client.remove_acl(path, a.get("roleid"), groups=a["ugid"])
            except Exception:
                pass
    return {"message": f"VNet '{vnet}' eliminada correctamente."}
