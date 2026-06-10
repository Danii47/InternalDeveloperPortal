"""
Comprobación de conectividad a internet de una VM recién desplegada.

Se ejecuta como tarea de fondo SIN `deployment_lock` (igual que app_installer):
espera al qemu-guest-agent y prueba una salida HTTP/ICMP real desde dentro de la
VM. El resultado se persiste en `vm_connectivity` para que el historial
(Live Task Stream del provisionamiento y ejecuciones de Blueprint) pueda mostrar
si la VM tiene internet — y por tanto si las apps del catálogo pudieron instalarse.
"""
import logging

import database
from core.deps import pve_client

logger = logging.getLogger("connectivity")


def check_vm_internet(node: str, vmid: int, vm_name: str) -> dict:
    """
    Función-tarea (la lanza `run_unlocked_task`). No lanza excepciones: si el
    guest-agent no responde, el resultado queda como `internet=None` (desconocido).
    """
    if not pve_client.agent_wait_ready(node, vmid):
        database.set_vm_connectivity(vm_name, None)
        logger.warning("Conectividad de %s: guest-agent no respondió", vm_name)
        return {"internet": None}

    ok = pve_client.agent_check_internet(node, vmid)
    database.set_vm_connectivity(vm_name, ok)
    logger.info("Conectividad de %s: %s", vm_name, "OK" if ok else "sin internet")
    return {"internet": ok}
