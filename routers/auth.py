import datetime
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


class LoginRequest(BaseModel):
    username: str
    realm: str       # "pve" or "pam"
    password: str


@router.post("/login")
def auth_login(body: LoginRequest, response: Response):
    """
    Validates credentials against Proxmox (pve/pam realm).
    On success creates an impersonation session and returns a JWT in an HttpOnly cookie.
    """
    _purge_stale_sessions()
    try:
        user_client = ProxmoxIDPClient.from_user(body.username, body.realm, body.password)
    except Exception:
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
