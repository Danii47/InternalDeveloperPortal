"""
Instalación de herramientas dentro de una VM recién creada, vía qemu-guest-agent.

Se ejecuta como tarea de fondo SIN `deployment_lock` (no es una operación de Terraform):
espera al agente, detecta el SO, calcula el plan desde `tool_catalog` y ejecuta cada paso
por la API de Proxmox. Reporta el resultado por herramienta; el resumen aparece en el
Live Task Stream.
"""
import logging

from core.deps import pve_client
from services import tool_catalog

logger = logging.getLogger("app_installer")


def install_apps(node: str, vmid: int, vm_name: str, tool_ids: list) -> None:
    """
    Función-tarea (la lanza `run_unlocked_task`). Lanza RuntimeError si algo va mal,
    para que la tarea quede marcada FAILED con un mensaje accionable.
    """
    if not tool_ids:
        return

    logger.info("Instalación en %s (vmid=%s): %s", vm_name, vmid, ", ".join(tool_ids))

    if not pve_client.agent_wait_ready(node, vmid):
        raise RuntimeError(
            "El guest-agent no respondió. ¿La plantilla incluye y arranca qemu-guest-agent?"
        )

    family = pve_client.agent_os_family(node, vmid)
    if family is None:
        raise RuntimeError(
            "SO no soportado para instalación automática (familias: debian/rhel/freebsd)."
        )

    plan = tool_catalog.get_install_plan(tool_ids, family)
    ok, failed, skipped = [], [], []

    for step in plan:
        label = step["name"]
        if step["command"] is None:
            skipped.append(label)
            logger.warning("Omitida (no soportada en %s): %s", family, label)
            continue
        try:
            code, out, err = pve_client.agent_run(node, vmid, step["command"])
            if code == 0:
                ok.append(label)
            else:
                failed.append(f"{label} (exit {code}: {(err or out)[:200].strip()})")
                logger.error("Fallo instalando %s en %s: %s", label, vm_name, err or out)
        except Exception as exc:
            failed.append(f"{label} ({exc})")
            logger.error("Error ejecutando %s en %s: %s", label, vm_name, exc)

    summary = "; ".join(
        part for part in (
            f"OK: {', '.join(ok)}" if ok else "",
            f"omitidas: {', '.join(skipped)}" if skipped else "",
            f"fallidas: {', '.join(failed)}" if failed else "",
        ) if part
    )
    logger.info("Instalación en %s [%s] → %s", vm_name, family, summary)

    if failed:
        raise RuntimeError(summary)
