"""
Router de Blueprints (orquestador), exclusivo de administradores.

Catálogo + constructor (CRUD) + ejecución:
- GET    /api/admin/blueprints              → catálogo (metadatos + variables)
- GET    /api/admin/blueprints/actions      → manifiesto de acciones (paleta del constructor)
- GET    /api/admin/blueprints/runs[/{id}]  → historial / estado de ejecución
- GET    /api/admin/blueprints/{id}         → definición completa (para editar)
- POST   /api/admin/blueprints              → crear blueprint de usuario
- PUT    /api/admin/blueprints/{id}         → editar (solo source='user')
- DELETE /api/admin/blueprints/{id}         → borrar (solo source='user')
- POST   /api/admin/blueprints/{id}/run     → validar inputs y lanzar la ejecución
"""
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
from core.deps import pve_client
from core.security import UserSession, require_admin
from services import actions, blueprint_loader, orchestrator, preflight as preflight_svc

router = APIRouter(prefix="/api/admin/blueprints", tags=["blueprints"])


class BlueprintDef(BaseModel):
    name: str
    description: str = ""
    on_failure: str = "halt"
    variables: list = []
    steps: list = []


class RunRequest(BaseModel):
    inputs: dict = {}
    rollback: Optional[bool] = None   # None → usa on_failure del blueprint


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:30] or "blueprint"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def _validate(bp: dict) -> None:
    try:
        blueprint_loader.validate_blueprint(bp)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ── Catálogo y manifiesto (rutas estáticas ANTES que /{id}) ─────────────────────

@router.get("")
def get_blueprints(sess: UserSession = Depends(require_admin)):
    return blueprint_loader.list_blueprints()


@router.get("/actions")
def get_actions(sess: UserSession = Depends(require_admin)):
    """Manifiesto declarativo de acciones para el constructor."""
    return actions.ACTION_SCHEMA


@router.get("/sdn-zones")
def get_sdn_zones(sess: UserSession = Depends(require_admin)):
    """Zonas SDN existentes (para el desplegable de create_vnet). No se crean zonas desde aquí."""
    return pve_client.get_sdn_zones()


@router.get("/runs")
def get_runs(sess: UserSession = Depends(require_admin)):
    return database.list_runs()


@router.get("/runs/{run_id}")
def get_run(run_id: str, sess: UserSession = Depends(require_admin)):
    run = database.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    return run


# ── CRUD de definiciones ─────────────────────────────────────────────────────────

@router.post("")
def create_blueprint(body: BlueprintDef, sess: UserSession = Depends(require_admin)):
    bp_id = _slug(body.name)
    bp = {"id": bp_id, "name": body.name, "description": body.description,
          "on_failure": body.on_failure, "variables": body.variables, "steps": body.steps}
    _validate(bp)
    database.create_blueprint(bp_id, body.name, body.description, sess.userid,
                              body.on_failure, body.variables, body.steps, source="user")
    return {"id": bp_id}


@router.get("/{blueprint_id}")
def get_blueprint(blueprint_id: str, sess: UserSession = Depends(require_admin)):
    row = database.get_blueprint_row(blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Blueprint no encontrado")
    return row


@router.put("/{blueprint_id}")
def update_blueprint(blueprint_id: str, body: BlueprintDef,
                     sess: UserSession = Depends(require_admin)):
    row = database.get_blueprint_row(blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Blueprint no encontrado")
    if row["source"] == "builtin":
        raise HTTPException(status_code=403, detail="Un blueprint integrado no se edita; duplícalo.")
    bp = {"id": blueprint_id, "name": body.name, "description": body.description,
          "on_failure": body.on_failure, "variables": body.variables, "steps": body.steps}
    _validate(bp)
    database.update_blueprint(blueprint_id, body.name, body.description,
                              body.on_failure, body.variables, body.steps)
    return {"id": blueprint_id}


@router.delete("/{blueprint_id}")
def delete_blueprint(blueprint_id: str, sess: UserSession = Depends(require_admin)):
    row = database.get_blueprint_row(blueprint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Blueprint no encontrado")
    if row["source"] == "builtin":
        raise HTTPException(status_code=403, detail="Un blueprint integrado no se borra.")
    database.delete_blueprint(blueprint_id)
    return {"deleted": blueprint_id}


# ── Ejecución ────────────────────────────────────────────────────────────────────

@router.post("/{blueprint_id}/preflight")
def preflight_blueprint(blueprint_id: str, body: RunRequest,
                        sess: UserSession = Depends(require_admin)):
    """Valida la ejecución contra la API de Proxmox sin crear nada (errors/warnings)."""
    bp = blueprint_loader.get_blueprint(blueprint_id)
    if bp is None:
        raise HTTPException(status_code=404, detail="Blueprint no encontrado")
    return preflight_svc.preflight(bp, body.inputs)


@router.post("/{blueprint_id}/run")
def run_blueprint(blueprint_id: str, body: RunRequest,
                  sess: UserSession = Depends(require_admin)):
    bp = blueprint_loader.get_blueprint(blueprint_id)
    if bp is None:
        raise HTTPException(status_code=404, detail="Blueprint no encontrado")

    try:
        ctx = blueprint_loader.validate_inputs(bp, body.inputs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Pre-flight obligatorio en servidor: si hay errores, no se ejecuta nada.
    checks = preflight_svc.preflight(bp, body.inputs)
    if checks["errors"]:
        raise HTTPException(status_code=422, detail={"message": "Pre-flight falló", "errors": checks["errors"]})

    orchestrator.compute_derived(ctx)

    use_rollback = (
        body.rollback if body.rollback is not None
        else bp.get("on_failure") == "rollback"
    )
    policy = "rollback" if use_rollback else "halt"

    secret_keys = {v["key"] for v in bp.get("variables", []) if v.get("secret")}
    stored_inputs = {k: ("***" if k in secret_keys else v) for k, v in body.inputs.items()}

    run_id = str(uuid.uuid4())
    database.create_run(run_id, bp["id"], bp["name"], sess.userid, stored_inputs, [])
    task_id = orchestrator.launch(run_id, bp, ctx, sess.userid, policy)

    return {"run_id": run_id, "task_id": task_id, "policy": policy}
