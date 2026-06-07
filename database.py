import sqlite3
import os
import time
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "QUOTAS_DB_PATH",
    os.path.join(os.path.dirname(__file__), "quotas.db"),
)

_DEFAULT_CPU = 32
_DEFAULT_RAM_MB = 32768   # 32 GB
_DEFAULT_DISK_GB = 500


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_quotas (
                pool_name   TEXT PRIMARY KEY,
                max_cpu     INTEGER NOT NULL DEFAULT 32,
                max_ram_mb  INTEGER NOT NULL DEFAULT 32768,
                max_disk_gb INTEGER NOT NULL DEFAULT 500
            )
        """)
        # Ephemeral environments (TTL / auto-destroy). vm_name is the natural key:
        # it equals the Terraform workspace and the key used by tf_runner.destroy().
        # expires_at NULL  → persistent ("Nunca"). status drives the reaper state
        # machine: active → expiring (grace window) → reaped.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vm_lifecycles (
                vm_name     TEXT PRIMARY KEY,
                vmid        INTEGER NOT NULL,
                node_name   TEXT    NOT NULL,
                owner       TEXT    NOT NULL,
                pool_name   TEXT    NOT NULL,
                created_at  REAL    NOT NULL,
                expires_at  REAL,
                status      TEXT    NOT NULL DEFAULT 'active'
            )
        """)
        # Blueprint executions (orchestrator runs). La DEFINICIÓN vive en YAML; aquí
        # solo se persiste el ESTADO/auditoría de cada ejecución. steps_json = lista
        # [{id, action, status, error, created_ref}]. status del run:
        # running | completed | failed | rolled_back | rollback_partial.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blueprint_runs (
                run_id       TEXT PRIMARY KEY,
                blueprint_id TEXT NOT NULL,
                name         TEXT NOT NULL,
                owner        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'running',
                inputs_json  TEXT NOT NULL DEFAULT '{}',
                steps_json   TEXT NOT NULL DEFAULT '[]',
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            )
        """)
        # Definiciones de Blueprints editables por el usuario (constructor visual). source:
        # 'builtin' (sembrado desde blueprints/*.yaml) | 'user' (creado en la UI).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blueprints (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                owner          TEXT NOT NULL DEFAULT '',
                on_failure     TEXT NOT NULL DEFAULT 'halt',
                variables_json TEXT NOT NULL DEFAULT '[]',
                steps_json     TEXT NOT NULL DEFAULT '[]',
                source         TEXT NOT NULL DEFAULT 'user',
                created_at     REAL NOT NULL,
                updated_at     REAL NOT NULL
            )
        """)


def get_or_create_quota(pool_name: str) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pool_quotas WHERE pool_name = ?", (pool_name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO pool_quotas (pool_name, max_cpu, max_ram_mb, max_disk_gb) "
                "VALUES (?, ?, ?, ?)",
                (pool_name, _DEFAULT_CPU, _DEFAULT_RAM_MB, _DEFAULT_DISK_GB),
            )
            return {
                "pool_name": pool_name,
                "max_cpu": _DEFAULT_CPU,
                "max_ram_mb": _DEFAULT_RAM_MB,
                "max_disk_gb": _DEFAULT_DISK_GB,
            }
        return dict(row)


def update_quota(pool_name: str, max_cpu: int, max_ram_mb: int, max_disk_gb: int) -> dict:
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO pool_quotas (pool_name, max_cpu, max_ram_mb, max_disk_gb)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pool_name) DO UPDATE SET
                max_cpu     = excluded.max_cpu,
                max_ram_mb  = excluded.max_ram_mb,
                max_disk_gb = excluded.max_disk_gb
        """, (pool_name, max_cpu, max_ram_mb, max_disk_gb))
    return {
        "pool_name": pool_name,
        "max_cpu": max_cpu,
        "max_ram_mb": max_ram_mb,
        "max_disk_gb": max_disk_gb,
    }


def list_all_quotas() -> list:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_quotas ORDER BY pool_name"
        ).fetchall()
        return [dict(r) for r in rows]


# ── VM lifecycles (ephemeral environments / TTL) ────────────────────────────────

def register_lifecycle(
    vm_name: str,
    vmid: int,
    node_name: str,
    owner: str,
    pool_name: str,
    expires_at: float | None,
) -> dict:
    """
    UPSERT a lifecycle row and (re)set it to 'active'. Called both at deploy time
    and on TTL renewal. expires_at=None marks the VM as persistent ("Nunca").
    """
    now = time.time()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO vm_lifecycles
                (vm_name, vmid, node_name, owner, pool_name, created_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(vm_name) DO UPDATE SET
                vmid       = excluded.vmid,
                node_name  = excluded.node_name,
                owner      = excluded.owner,
                pool_name  = excluded.pool_name,
                expires_at = excluded.expires_at,
                status     = 'active'
        """, (vm_name, vmid, node_name, owner, pool_name, now, expires_at))
    return {
        "vm_name": vm_name,
        "vmid": vmid,
        "node_name": node_name,
        "owner": owner,
        "pool_name": pool_name,
        "expires_at": expires_at,
        "status": "active",
    }


def get_lifecycle(vm_name: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vm_lifecycles WHERE vm_name = ?", (vm_name,)
        ).fetchone()
        return dict(row) if row else None


def mark_status(vm_name: str, status: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE vm_lifecycles SET status = ? WHERE vm_name = ?", (status, vm_name)
        )


def delete_lifecycle(vm_name: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM vm_lifecycles WHERE vm_name = ?", (vm_name,))


def list_due_for_expiring(now: float) -> list:
    """Active rows whose TTL has elapsed — candidates to enter the grace window."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM vm_lifecycles
            WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
        """, (now,)).fetchall()
        return [dict(r) for r in rows]


def list_due_for_reaping(now: float, grace_seconds: float) -> list:
    """Expiring rows whose grace window has also elapsed — ready to be destroyed."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM vm_lifecycles
            WHERE status = 'expiring' AND expires_at IS NOT NULL
              AND (expires_at + ?) <= ?
        """, (grace_seconds, now)).fetchall()
        return [dict(r) for r in rows]


def requeue_stale_reaping() -> int:
    """
    Reconciliación de arranque: revierte a 'expiring' cualquier fila atascada en el
    estado transitorio 'reaping' (su tarea de destrucción se perdió en un reinicio,
    porque la cola del TaskManager es en memoria). Así el reaper la reintenta.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE vm_lifecycles SET status = 'expiring' WHERE status = 'reaping'"
        )
        return cur.rowcount


def get_expiry_map() -> dict:
    """{vm_name: expires_at} for every non-reaped lifecycle — enriches the inventory."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT vm_name, expires_at FROM vm_lifecycles WHERE status != 'reaped'"
        ).fetchall()
        return {r["vm_name"]: r["expires_at"] for r in rows}


# ── Blueprint runs (orquestador) ────────────────────────────────────────────────

import json as _json


def create_run(run_id: str, blueprint_id: str, name: str, owner: str,
               inputs: dict, steps: list) -> None:
    """Registra una ejecución de blueprint en estado 'running'."""
    now = time.time()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO blueprint_runs
                (run_id, blueprint_id, name, owner, status, inputs_json, steps_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
        """, (run_id, blueprint_id, name, owner,
              _json.dumps(inputs), _json.dumps(steps), now, now))


def update_run(run_id: str, status: str = None, steps: list = None) -> None:
    """Actualiza estado y/o el log de pasos de una ejecución."""
    sets, params = [], []
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if steps is not None:
        sets.append("steps_json = ?"); params.append(_json.dumps(steps))
    sets.append("updated_at = ?"); params.append(time.time())
    params.append(run_id)
    with _get_conn() as conn:
        conn.execute(f"UPDATE blueprint_runs SET {', '.join(sets)} WHERE run_id = ?", params)


def get_run(run_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM blueprint_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["inputs"] = _json.loads(d.pop("inputs_json"))
        d["steps"] = _json.loads(d.pop("steps_json"))
        return d


def list_runs(owner: str = None, limit: int = 50) -> list:
    """Ejecuciones recientes; si se da `owner`, solo las suyas."""
    with _get_conn() as conn:
        if owner is not None:
            rows = conn.execute(
                "SELECT * FROM blueprint_runs WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM blueprint_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["inputs"] = _json.loads(d.pop("inputs_json"))
            d["steps"] = _json.loads(d.pop("steps_json"))
            out.append(d)
        return out


# ── Blueprints (definiciones editables) ─────────────────────────────────────────

def create_blueprint(bp_id: str, name: str, description: str, owner: str,
                     on_failure: str, variables: list, steps: list,
                     source: str = "user") -> None:
    now = time.time()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO blueprints
                (id, name, description, owner, on_failure, variables_json, steps_json, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bp_id, name, description, owner, on_failure,
              _json.dumps(variables), _json.dumps(steps), source, now, now))


def update_blueprint(bp_id: str, name: str, description: str,
                     on_failure: str, variables: list, steps: list) -> None:
    with _get_conn() as conn:
        conn.execute("""
            UPDATE blueprints SET name = ?, description = ?, on_failure = ?,
                variables_json = ?, steps_json = ?, updated_at = ?
            WHERE id = ?
        """, (name, description, on_failure, _json.dumps(variables),
              _json.dumps(steps), time.time(), bp_id))


def delete_blueprint(bp_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM blueprints WHERE id = ?", (bp_id,))


def _row_to_blueprint(row) -> dict:
    d = dict(row)
    d["variables"] = _json.loads(d.pop("variables_json"))
    d["steps"] = _json.loads(d.pop("steps_json"))
    return d


def get_blueprint_row(bp_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM blueprints WHERE id = ?", (bp_id,)).fetchone()
        return _row_to_blueprint(row) if row else None


def list_blueprint_rows() -> list:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM blueprints ORDER BY name").fetchall()
        return [_row_to_blueprint(r) for r in rows]
