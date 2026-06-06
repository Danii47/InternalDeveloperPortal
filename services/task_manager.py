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


def _background_worker() -> None:
    while True:
        task = task_queue.get()
        task_id: str = task["id"]
        func: Callable = task["func"]

        tasks_db[task_id]["status"] = TaskStatus.RUNNING
        tasks_db[task_id]["updated_at"] = time.time()

        try:
            with deployment_lock:
                func()
            tasks_db[task_id]["status"] = TaskStatus.COMPLETED
        except Exception as exc:
            tasks_db[task_id]["status"] = TaskStatus.FAILED
            tasks_db[task_id]["error"] = str(exc)
        finally:
            tasks_db[task_id]["updated_at"] = time.time()
            task_queue.task_done()


threading.Thread(target=_background_worker, daemon=True, name="TF_Worker").start()


def create_and_queue_task(
    task_type: str,
    target_name: str,
    func: Callable,
    owner: Optional[str] = None,
) -> str:
    """Registers a task in tasks_db and places it on the queue. Returns the task_id."""
    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {
        "id": task_id,
        "type": task_type,
        "target": target_name,
        "status": TaskStatus.PENDING,
        "error": None,
        "owner": owner,
        "updated_at": time.time(),
    }
    task_queue.put({"id": task_id, "func": func})
    return task_id


def _register_task(task_type: str, target_name: str, owner: Optional[str]) -> str:
    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {
        "id": task_id,
        "type": task_type,
        "target": target_name,
        "status": TaskStatus.PENDING,
        "error": None,
        "owner": owner,
        "updated_at": time.time(),
    }
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

    def _runner() -> None:
        tasks_db[task_id]["status"] = TaskStatus.RUNNING
        tasks_db[task_id]["updated_at"] = time.time()
        try:
            func()
            tasks_db[task_id]["status"] = TaskStatus.COMPLETED
        except Exception as exc:
            tasks_db[task_id]["status"] = TaskStatus.FAILED
            tasks_db[task_id]["error"] = str(exc)
        finally:
            tasks_db[task_id]["updated_at"] = time.time()

    _async_pool.submit(_runner)
    return task_id
