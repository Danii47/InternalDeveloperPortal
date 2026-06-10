from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
from core.security import UserSession, get_current_session, require_admin
from services.proxmox_client import ProxmoxIDPClient

router = APIRouter(prefix="/api", tags=["admin", "quotas"])


class UpdateQuotaRequest(BaseModel):
    max_cpu: int
    max_ram_mb: int
    max_disk_gb: int


class AddMemberRequest(BaseModel):
    userid: str


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
