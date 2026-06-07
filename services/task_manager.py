"""
Asynchronous task queue backed by a single background thread.

All Terraform operations are serialised through `deployment_lock` to prevent
concurrent workspace mutations. Callers enqueue work via `create_and_queue_task`
and poll `tasks_db` for status updates.

Non-Terraform work that must NOT block the queue (e.g. guest-agent app installs,
which are pure Proxmox API calls and can run for minutes) goes through
`run_unlocked_task`: it runs in a thread pool WITHOUT `deployment_lock`, while
still surfacing in `tasks_db` (and therefore in the Live Task Stream).
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from queue import Queue
from typing import Any, Callable, Dict, Optional


class TaskStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"


tasks_db: Dict[str, Dict[str, Any]] = {}
task_queue: Queue = Queue()
deployment_lock = threading.Lock()

# Pool para tareas que NO deben tomar deployment_lock (instalaciones por guest-agent).
_async_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")


def _register_task(task_type: str, target_name: str, owner: Optional[str]) -> str:
    """Crea la entrada en tasks_db con su Event de finalización. Devuelve el task_id."""
    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {
        "id": task_id,
        "type": task_type,
        "target": target_name,
        "status": TaskStatus.PENDING,
        "error": None,
        "owner": owner,
        "result": None,          # valor devuelto por func (p.ej. ref del recurso creado)
        "updated_at": time.time(),
        "_event": threading.Event(),  # privado: lo consume wait_for_task; se excluye del JSON
    }
    return task_id


def _run_task(task_id: str, func: Callable) -> None:
    """Ejecuta func actualizando estado/result y señalizando el Event al terminar."""
    entry = tasks_db[task_id]
    entry["status"] = TaskStatus.RUNNING
    entry["updated_at"] = time.time()
    try:
        entry["result"] = func()
        entry["status"] = TaskStatus.COMPLETED
    except Exception as exc:
        entry["status"] = TaskStatus.FAILED
        entry["error"] = str(exc)
    finally:
        entry["updated_at"] = time.time()
        entry["_event"].set()


def _background_worker() -> None:
    while True:
        task = task_queue.get()
        try:
            with deployment_lock:
                _run_task(task["id"], task["func"])
        finally:
            task_queue.task_done()


threading.Thread(target=_background_worker, daemon=True, name="TF_Worker").start()


def create_and_queue_task(
    task_type: str,
    target_name: str,
    func: Callable,
    owner: Optional[str] = None,
) -> str:
    """Registra un task y lo encola en la cola serializada (deployment_lock). Devuelve task_id."""
    task_id = _register_task(task_type, target_name, owner)
    task_queue.put({"id": task_id, "func": func})
    return task_id


def run_unlocked_task(
    task_type: str,
    target_name: str,
    func: Callable,
    owner: Optional[str] = None,
) -> str:
    """
    Ejecuta `func` en el pool asíncrono SIN `deployment_lock` (no es Terraform).
    Se registra en tasks_db igual que las demás → visible en el Live Task Stream.
    """
    task_id = _register_task(task_type, target_name, owner)
    _async_pool.submit(_run_task, task_id, func)
    return task_id


def run_inline_task(
    task_type: str,
    target_name: str,
    func: Callable,
    owner: Optional[str] = None,
) -> tuple:
    """
    Ejecuta `func` SÍNCRONAMENTE en el hilo del llamante (no usa pools ni el lock).
    Lo registra en tasks_db (visible en el Live Task Stream) y devuelve (status, error, result).
    Pensado para pasos de Blueprint que son API pura: evita ocupar un worker del pool
    mientras el orquestador (que ya corre en el pool) espera → sin riesgo de deadlock.
    """
    task_id = _register_task(task_type, target_name, owner)
    _run_task(task_id, func)
    entry = tasks_db[task_id]
    return entry["status"], entry.get("error"), entry.get("result")


def wait_for_task(task_id: str, timeout: float = 3600) -> tuple:
    """
    Bloquea hasta que el task alcance estado terminal (vía su Event, sin polling).
    Devuelve (status, error, result). Usado por el orquestador de Blueprints para
    secuenciar pasos sin retener `deployment_lock`.
    """
    entry = tasks_db.get(task_id)
    if entry is None:
        return TaskStatus.FAILED, "Task no encontrado", None
    if not entry["_event"].wait(timeout):
        return TaskStatus.FAILED, f"Timeout tras {timeout}s esperando la tarea", None
    return entry["status"], entry.get("error"), entry.get("result")
