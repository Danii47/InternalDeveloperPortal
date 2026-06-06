import asyncio
import ssl
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from websockets.asyncio.client import connect as ws_connect

from core.config import ALLOWED_ORIGINS, PROXMOX_HOST, PROXMOX_PORT
from core.security import (
    UserSession,
    assert_vm_accessible_by_vmid,
    get_current_session,
    get_session_from_ws,
)

router = APIRouter(prefix="/api", tags=["console"])


@router.post("/vms/{node}/{vmid}/console")
async def init_console(
    node: str,
    vmid: int,
    engine: str = Query("vnc"),
    sess: UserSession = Depends(get_current_session),
):
    if engine not in ("vnc", "serial"):
        raise HTTPException(400, "engine debe ser 'vnc' o 'serial'")

    assert_vm_accessible_by_vmid(sess, vmid)

    if engine == "vnc":
        proxy = sess.client.open_vncproxy(node, vmid)
        return {
            "engine": "vnc",
            "port": int(proxy["port"]),
            "ticket": proxy["ticket"],
            "serial_available": sess.client.has_serial_console(node, vmid),
        }

    if not sess.client.has_serial_console(node, vmid):
        raise HTTPException(400, "Esta VM no tiene consola serie configurada")
    proxy = sess.client.open_termproxy(node, vmid)
    return {
        "engine": "serial",
        "port": int(proxy["port"]),
        "ticket": proxy["ticket"],
        "user": proxy.get("user", ""),
    }


@router.websocket("/vms/{node}/{vmid}/vncwebsocket")
async def console_ws(
    websocket: WebSocket,
    node: str,
    vmid: int,
    port: int = Query(...),
    vncticket: str = Query(...),
):
    origin = websocket.headers.get("origin", "")
    if ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return

    sess = get_session_from_ws(websocket)
    if not sess:
        await websocket.close(code=1008)
        return

    try:
        assert_vm_accessible_by_vmid(sess, vmid)
    except HTTPException:
        await websocket.close(code=1008)
        return

    pve_ticket, _ = sess.client.proxmox.get_tokens()

    pve_url = (
        f"wss://{PROXMOX_HOST}:{PROXMOX_PORT}"
        f"/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket"
        f"?port={port}&vncticket={quote(vncticket, safe='')}"
    )

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    await websocket.accept(subprotocol="binary")

    try:
        async with ws_connect(
            pve_url,
            ssl=ssl_ctx,
            additional_headers={"Cookie": f"PVEAuthCookie={pve_ticket}"},
            subprotocols=["binary"],
        ) as pve_ws:

            async def b2p() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if msg.get("bytes"):
                            await pve_ws.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await pve_ws.send(msg["text"])
                except Exception:
                    pass

            async def p2b() -> None:
                try:
                    async for data in pve_ws:
                        if isinstance(data, str):
                            await websocket.send_text(data)
                        else:
                            await websocket.send_bytes(data)
                except Exception:
                    pass

            t1 = asyncio.create_task(b2p())
            t2 = asyncio.create_task(p2b())
            try:
                await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            finally:
                t1.cancel()
                t2.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)

    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
