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


def _build_quota_entry(pool_name: str, client) -> dict:
    return {
        "pool_name": pool_name,
        "quota": database.get_or_create_quota(pool_name),
        "usage": client.get_pool_usage(pool_name),
    }


@router.get("/quotas")
def get_user_quotas(sess: UserSession = Depends(get_current_session)):
    pool_name = ProxmoxIDPClient._poolid_for(sess.userid)
    return [_build_quota_entry(pool_name, sess.client)]


@router.get("/admin/quotas")
def get_all_quotas(sess: UserSession = Depends(require_admin)):
    all_pools = sess.client.get_all_pools()
    return [_build_quota_entry(p, sess.client) for p in all_pools]


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
