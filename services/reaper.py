"""
TTL Reaper — background sweeper that auto-destroys expired ephemeral VMs.

Design constraints (mirrors task_manager conventions):
  * Runs in a single daemon thread started as an import side-effect.
  * The sweep itself NEVER holds `deployment_lock`; it only reads the DB and
    *enqueues* destroy jobs via `create_and_queue_task`. The actual Terraform
    destroy then runs inside the worker under the lock, so it can never race a
    user deploy and corrupt a workspace.
  * Fail-safe: any discrepancy between the DB and the live cluster (VM gone,
    name mismatch, pool mismatch, NULL expiry) → skip + log, never destroy.

State machine per VM: active → expiring (grace window) → reaped.
"""
import logging
import threading
import time

import database
from core.config import REAPER_ENABLED, REAPER_GRACE_HOURS, REAPER_INTERVAL
from core.deps import pve_client, tf_runner
from services.task_manager import create_and_queue_task

log = logging.getLogger("ttl_reaper")


def _blindaje_ok(c: dict) -> bool:
    """
    Triple in-vivo verification against Proxmox before a VM is destroyed.
    Returns True only if it is safe to reap. Any mismatch is logged and skipped.
    """
    # 1) Defensive re-assertion: never touch a persistent VM.
    if c["expires_at"] is None:
        log.warning("Reaper: %s sin expires_at, omitida", c["vm_name"])
        return False

    res = pve_client.get_vm_resource(c["vmid"])

    # 2) The VM must still exist. If it's gone, there's nothing to destroy —
    #    clean up the stale row so we stop tracking it.
    if res is None:
        log.info("Reaper: %s (vmid=%s) ya no existe; limpiando registro",
                 c["vm_name"], c["vmid"])
        database.delete_lifecycle(c["vm_name"])
        return False

    # 3) Name must match what we registered. Guards against vmid reuse or any
    #    DB↔cluster desync.
    if res.get("name") != c["vm_name"]:
        log.warning("Reaper: nombre no coincide para vmid=%s (cluster=%r, db=%r); omitida",
                    c["vmid"], res.get("name"), c["vm_name"])
        return False

    # 4) Pool must match too — pero solo si cluster/resources nos devuelve ese dato.
    #    El campo `pool` requiere el privilegio `Pool.Audit`, que NO forma parte de
    #    los privilegios mínimos documentados para el token (blueprints/README.md);
    #    con el token estándar Proxmox devuelve `pool: null` para TODAS las VMs,
    #    lo que haría fallar esta comprobación SIEMPRE y dejaría la VM en
    #    'expiring' para siempre. Si el token sí tiene Pool.Audit, se sigue
    #    aplicando la comprobación normalmente.
    cluster_pool = res.get("pool")
    if cluster_pool is not None and cluster_pool != c["pool_name"]:
        log.warning("Reaper: pool no coincide para %s (cluster=%r, db=%r); omitida",
                    c["vm_name"], cluster_pool, c["pool_name"])
        return False

    return True


def _sweep() -> None:
    now = time.time()
    grace_seconds = REAPER_GRACE_HOURS * 3600

    # Phase 1 — TTL elapsed: move active rows into the grace window.
    for c in database.list_due_for_expiring(now):
        database.mark_status(c["vm_name"], "expiring")
        log.info("Reaper: TTL expirado para %s; gracia de %sh antes de destruir",
                 c["vm_name"], REAPER_GRACE_HOURS)

    # Phase 2 — grace elapsed: enqueue destruction for verified VMs.
    for c in database.list_due_for_reaping(now, grace_seconds):
        if not _blindaje_ok(c):
            continue

        vm_name, vmid, node, owner = (
            c["vm_name"], c["vmid"], c["node_name"], c["owner"]
        )

        def job(vm_name=vm_name, vmid=vmid, node=node):
            try:
                tf_runner.destroy(vm_name, vmid, node)
                database.delete_lifecycle(vm_name)
            except Exception:
                # Revert so the next sweep retries instead of orphaning the VM.
                database.mark_status(vm_name, "expiring")
                raise

        # Transient state 'reaping' is NOT selected by list_due_for_reaping (which
        # only matches 'expiring'), so we never enqueue duplicates while the destroy
        # task is pending/running. On failure the job reverts it to 'expiring'.
        database.mark_status(vm_name, "reaping")
        create_and_queue_task("Auto-Destrucción (TTL)", vm_name, job, owner=owner)
        log.info("Reaper: encolada auto-destrucción de %s (owner=%s)", vm_name, owner)


def _reaper_loop() -> None:
    while True:
        try:
            _sweep()
        except Exception as exc:
            log.error("Reaper sweep falló: %s", exc)
        time.sleep(REAPER_INTERVAL)


if REAPER_ENABLED:
    # Recover from a restart mid-destruction: revert any stuck 'reaping' rows.
    try:
        database.requeue_stale_reaping()
    except Exception as exc:
        log.error("Reaper: no se pudo reconciliar filas 'reaping': %s", exc)
    threading.Thread(target=_reaper_loop, daemon=True, name="TTL_Reaper").start()
