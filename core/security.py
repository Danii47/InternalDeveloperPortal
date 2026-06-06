"""
JWT session management and RBAC guards.

Two kinds of objects live here:
  - Stateful: the in-process `sessions` dict and helpers that touch it.
  - FastAPI dependencies: `get_current_session`, `optional_session`, `require_admin`.
  - Auth gates: `assert_*` functions consumed by router handlers.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, TYPE_CHECKING

import jwt
from fastapi import Depends, HTTPException, Request, WebSocket

from core.config import SECRET_KEY, SESSION_TTL

if TYPE_CHECKING:
    from services.proxmox_client import ProxmoxIDPClient


# ── Session model ─────────────────────────────────────────────────────────────

@dataclass
class UserSession:
    client: "ProxmoxIDPClient"   # impersonated Proxmox ticket client
    userid: str                  # e.g. "daniel@pve"
    realm: str                   # "pve" or "pam"
    jti: str                     # JWT ID — key in the sessions dict
    last_used: float = field(default_factory=time.time)


sessions: Dict[str, UserSession] = {}


# ── Session helpers ───────────────────────────────────────────────────────────

def _purge_stale_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    stale = [jti for jti, s in sessions.items() if s.last_used < cutoff]
    for jti in stale:
        del sessions[jti]


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def get_current_session(request: Request) -> UserSession:
    token = request.cookies.get("idp_token")
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sesión expirada, vuelve a iniciar sesión")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

    jti = payload.get("jti")
    sess = sessions.get(jti)
    if not sess:
        raise HTTPException(status_code=401, detail="Sesión no encontrada, vuelve a iniciar sesión")

    sess.last_used = time.time()
    return sess


def optional_session(request: Request) -> Optional[UserSession]:
    """Returns a session if the cookie is valid; None otherwise. Never raises."""
    token = request.cookies.get("idp_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        jti = payload.get("jti")
        sess = sessions.get(jti)
        if sess:
            sess.last_used = time.time()
        return sess
    except Exception:
        return None


def get_session_from_ws(websocket: WebSocket) -> Optional[UserSession]:
    token = websocket.cookies.get("idp_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        jti = payload.get("jti")
        sess = sessions.get(jti)
        if sess:
            sess.last_used = time.time()
        return sess
    except Exception:
        return None


# ── Admin check ───────────────────────────────────────────────────────────────

def _is_admin(sess: UserSession) -> bool:
    """PAM-realm users (root@pam and other local accounts) are global admins."""
    return sess.realm == "pam"


def require_admin(sess: UserSession = Depends(get_current_session)) -> UserSession:
    if not _is_admin(sess):
        raise HTTPException(status_code=403, detail="Solo administradores pueden realizar esta acción")
    return sess


# ── RBAC authorization gates ──────────────────────────────────────────────────

def assert_vm_accessible_by_vmid(sess: UserSession, vmid: int) -> None:
    inventory = sess.client.get_inventory()
    if not any(vm.get("vmid") == vmid for vm in inventory):
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado: la VM {vmid} no está en tu inventario",
        )


def assert_vm_accessible_by_name(sess: UserSession, vm_name: str) -> None:
    inventory = sess.client.get_inventory()
    if not any(vm.get("name") == vm_name for vm in inventory):
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado: la VM '{vm_name}' no está en tu inventario",
        )


def assert_deploy_params_allowed(
    sess: UserSession, node: str, template_id: int, network_bridge: str
) -> None:
    allowed_nodes = sess.client.get_nodes()
    if node not in allowed_nodes:
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado: el nodo '{node}' no está en tu inventario",
        )

    allowed_templates = sess.client.get_templates(node)
    if template_id not in allowed_templates.values():
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado: la plantilla ID {template_id} no está disponible en '{node}'",
        )

    allowed_networks = sess.client.get_networks(node)
    allowed_bridges = set(allowed_networks.get("bridges", [])) | set(allowed_networks.get("vnets", []))
    if network_bridge not in allowed_bridges:
        raise HTTPException(
            status_code=403,
            detail=f"Acceso denegado: la red '{network_bridge}' no está disponible en '{node}'",
        )
