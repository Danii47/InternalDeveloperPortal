import datetime
import threading
import time
import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core.config import JWT_EXPIRY_H, SECRET_KEY, SECURE_COOKIE
from core.deps import pve_client
from core.security import UserSession, _purge_stale_sessions, get_current_session, sessions
from services.proxmox_client import ProxmoxIDPClient

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── Rate-limiting de login (anti fuerza-bruta) ──────────────────────────────────
# Cuenta SOLO los intentos FALLIDOS por IP en una ventana; un login correcto no acumula.
_LOGIN_WINDOW = 300      # segundos
_LOGIN_MAX_FAILS = 10    # fallos permitidos por IP y ventana
_login_fails: dict = {}  # ip -> [timestamps de fallos]
_login_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
        _login_fails[ip] = recent
        return len(recent) >= _LOGIN_MAX_FAILS


def _record_fail(ip: str) -> None:
    with _login_lock:
        _login_fails.setdefault(ip, []).append(time.time())


class LoginRequest(BaseModel):
    username: str
    realm: str       # "pve" or "pam"
    password: str


@router.post("/login")
def auth_login(body: LoginRequest, response: Response, request: Request):
    """
    Validates credentials against Proxmox (pve/pam realm).
    On success creates an impersonation session and returns a JWT in an HttpOnly cookie.
    """
    ip = _client_ip(request)
    if _too_many_fails(ip):
        raise HTTPException(
            status_code=429,
            detail="Demasiados intentos fallidos. Espera unos minutos e inténtalo de nuevo.",
        )

    _purge_stale_sessions()
    try:
        user_client = ProxmoxIDPClient.from_user(body.username, body.realm, body.password)
    except Exception:
        _record_fail(ip)
        raise HTTPException(
            status_code=401,
            detail="Credenciales inválidas o usuario sin acceso a Proxmox",
        )

    jti = str(uuid.uuid4())
    userid = f"{body.username}@{body.realm}"
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": userid,
        "jti": jti,
        "realm": body.realm,
        "iat": now,
        "exp": now + datetime.timedelta(hours=JWT_EXPIRY_H),
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

    sessions[jti] = UserSession(
        client=user_client,
        userid=userid,
        realm=body.realm,
        jti=jti,
    )

    response.set_cookie(
        key="idp_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIE,
        max_age=JWT_EXPIRY_H * 3600,
    )
    return {"message": "Login exitoso", "userid": userid}


@router.post("/logout")
def auth_logout(request: Request, response: Response):
    """Invalidates the server-side session and clears the cookie."""
    token = request.cookies.get("idp_token")
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            jti = payload.get("jti")
            if jti in sessions:
                del sessions[jti]
        except Exception:
            pass
    response.delete_cookie("idp_token", samesite="lax", secure=SECURE_COOKIE)
    return {"message": "Sesión cerrada correctamente"}


@router.get("/me")
def auth_me(sess: UserSession = Depends(get_current_session)):
    """Returns the authenticated user's identity (consumed by the Astro middleware)."""
    return {"userid": sess.userid, "realm": sess.realm}
