"""
Carga, validación y siembra de Blueprints.

Las DEFINICIONES viven ahora en la tabla `blueprints` (editables desde el constructor de la UI).
Los `blueprints/*.yaml` del repo son SEMILLAS: en el arranque, `seed_builtins()` importa a la BD
los que aún no existan (source='builtin'). A partir de ahí todo se gestiona en BD.

Responsabilidades:
  - list_blueprints() / get_blueprint(id)  → catálogo y definición (desde BD)
  - validate_blueprint(bp)                 → valida esquema + params contra el manifiesto de acciones
  - validate_inputs(bp, inputs)            → valida/coacciona los inputs del usuario al ejecutar (422)
  - resolve(obj, ctx)                      → sustitución literal {{key}} (sin eval)
  - seed_builtins()                        → importa los YAML a BD (idempotente)

Seguridad: `action` ∈ conjunto CERRADO (services.actions.ALLOWED_ACTIONS); los params se validan
contra ACTION_SCHEMA; los placeholders solo pueden referenciar variables declaradas o derivadas.
"""
import ipaddress
import logging
import os
import re

import yaml

import database
from services import actions

logger = logging.getLogger("blueprint_loader")

BLUEPRINTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "blueprints")

ALLOWED_ACTIONS = actions.ALLOWED_ACTIONS

# Tipos de variable admitidos en el formulario de ejecución.
VAR_TYPES = {"string", "cidr", "int", "slider", "select", "node_select", "template_select",
             "text", "bridge_multi", "tool_multi", "vm_name", "secret", "bool"}

# Claves DERIVADAS que inyecta la plataforma (ver orchestrator.compute_derived).
DERIVED_KEYS = {"vnet_id"}

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_KEY_RE = re.compile(r"^[a-zA-Z0-9_]+$")


# ── Validación ───────────────────────────────────────────────────────────────────

def _iter_placeholders(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_placeholders(v)
    elif isinstance(obj, str):
        yield from _PLACEHOLDER.findall(obj)


def _is_empty(val) -> bool:
    return (val is None
            or (isinstance(val, str) and val.strip() == "")
            or (isinstance(val, list) and len(val) == 0))


def _validate_step_params(step: dict) -> None:
    """Valida los params de un step contra ACTION_SCHEMA de su acción."""
    schema = actions.ACTION_SCHEMA[step["action"]]
    pdefs = {p["key"]: p for p in schema["params"]}
    params = step.get("params", {})

    for k in params:
        if k not in pdefs:
            raise ValueError(f"step '{step['id']}': param desconocido '{k}'")

    for p in schema["params"]:
        val = params.get(p["key"])
        if p.get("required") and _is_empty(val):
            raise ValueError(f"step '{step['id']}': falta el param requerido '{p['key']}'")
        # Validación de enteros literales (los {{placeholder}} se difieren a runtime).
        if (not _is_empty(val) and p["type"] in actions.INT_TYPES
                and isinstance(val, str) and "{{" not in val):
            try:
                int(val)
            except ValueError:
                raise ValueError(f"step '{step['id']}': '{p['key']}' debe ser un entero")


def validate_blueprint(bp: dict) -> None:
    """Lanza ValueError si la receta no cumple el esquema (estructura + params + placeholders)."""
    if not isinstance(bp, dict):
        raise ValueError("la definición debe ser un objeto")
    for field in ("id", "name"):
        if not bp.get(field):
            raise ValueError(f"falta el campo obligatorio '{field}'")
    if not _KEY_RE.match(str(bp["id"]).replace("-", "_")):
        raise ValueError("'id' inválido (solo letras, números, '-' y '_')")
    if bp.get("on_failure", "halt") not in ("halt", "rollback"):
        raise ValueError("on_failure debe ser 'halt' o 'rollback'")

    variables = bp.get("variables", [])
    steps = bp.get("steps", [])
    if not isinstance(variables, list):
        raise ValueError("'variables' debe ser una lista")
    if not isinstance(steps, list) or not steps:
        raise ValueError("'steps' debe ser una lista no vacía")

    declared = set()
    for v in variables:
        key = v.get("key")
        if not key or not _KEY_RE.match(key):
            raise ValueError(f"variable con 'key' inválida: {key!r}")
        if v.get("type") not in VAR_TYPES:
            raise ValueError(f"variable '{key}': type '{v.get('type')}' no soportado")
        declared.add(key)

    for s in steps:
        if not s.get("id"):
            raise ValueError("un step no tiene 'id'")
        if s.get("action") not in ALLOWED_ACTIONS:
            raise ValueError(f"step '{s.get('id')}': action '{s.get('action')}' no permitida")
        if not isinstance(s.get("params", {}), dict):
            raise ValueError(f"step '{s.get('id')}': 'params' debe ser un mapping")
        _validate_step_params(s)

    used = set(_iter_placeholders(steps))
    unknown = used - declared - DERIVED_KEYS
    if unknown:
        raise ValueError(f"placeholders sin variable declarada: {', '.join(sorted(unknown))}")


# Alias retro-compat (el orquestador/tests usaban _validate_blueprint).
_validate_blueprint = validate_blueprint


# ── Acceso (desde BD) ─────────────────────────────────────────────────────────────

def list_blueprints() -> list:
    """Metadatos para el catálogo/form de ejecución (incluye variables y source/owner)."""
    return [
        {
            "id": bp["id"], "name": bp["name"], "description": bp.get("description", ""),
            "on_failure": bp.get("on_failure", "halt"), "source": bp.get("source", "user"),
            "owner": bp.get("owner", ""), "variables": bp.get("variables", []),
        }
        for bp in database.list_blueprint_rows()
    ]


def get_blueprint(blueprint_id: str) -> dict | None:
    return database.get_blueprint_row(blueprint_id)


# ── Siembra de builtins ───────────────────────────────────────────────────────────

def seed_builtins() -> None:
    """Importa a BD los blueprints YAML del repo que aún no existan (idempotente)."""
    if not os.path.isdir(BLUEPRINTS_DIR):
        return
    for fname in sorted(os.listdir(BLUEPRINTS_DIR)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        try:
            with open(os.path.join(BLUEPRINTS_DIR, fname)) as f:
                bp = yaml.safe_load(f)
            validate_blueprint(bp)
            if database.get_blueprint_row(bp["id"]) is None:
                database.create_blueprint(
                    bp["id"], bp["name"], bp.get("description", ""), "system",
                    bp.get("on_failure", "halt"), bp.get("variables", []), bp["steps"],
                    source="builtin",
                )
                logger.info("Blueprint builtin sembrado: %s", bp["id"])
        except Exception as exc:
            logger.error("No se pudo sembrar el blueprint %s: %s", fname, exc)


# ── Validación de inputs del usuario (al ejecutar) ───────────────────────────────

def validate_inputs(bp: dict, inputs: dict) -> dict:
    """
    Valida/coacciona los inputs contra `variables`. Devuelve el contexto de resolución o
    lanza ValueError (→ 422) con el primer problema encontrado.
    """
    if not isinstance(inputs, dict):
        raise ValueError("inputs debe ser un objeto")
    ctx: dict = {}
    for v in bp.get("variables", []):
        key, vtype = v["key"], v["type"]
        label = v.get("label", key)
        required = v.get("required", True)
        raw = inputs.get(key, None)

        if _is_empty(raw):
            if "default" in v:
                ctx[key] = v["default"]
                continue
            if required:
                raise ValueError(f"Falta el campo obligatorio: {label}")
            continue

        if vtype in ("int", "slider"):
            try:
                num = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{label} debe ser un número entero")
            if "min" in v and num < v["min"]:
                raise ValueError(f"{label} debe ser ≥ {v['min']}")
            if "max" in v and num > v["max"]:
                raise ValueError(f"{label} debe ser ≤ {v['max']}")
            ctx[key] = num

        elif vtype == "cidr":
            try:
                ipaddress.ip_network(str(raw), strict=False)
            except ValueError:
                raise ValueError(f"{label}: CIDR inválido (ej. 10.20.0.1/24)")
            ctx[key] = str(raw).strip()

        elif vtype == "select":
            allowed = {(o["value"] if isinstance(o, dict) else o) for o in v.get("options", [])}
            if str(raw) not in {str(a) for a in allowed}:
                raise ValueError(f"{label}: opción no válida")
            ctx[key] = raw

        elif vtype in actions.MULTI_TYPES:
            ctx[key] = raw if isinstance(raw, list) else [s.strip() for s in str(raw).split(",") if s.strip()]

        else:  # string, text, node_select, template_select, vm_name, secret, bool
            s = str(raw).strip()
            pattern = v.get("pattern")
            if pattern and not re.fullmatch(pattern, s):
                raise ValueError(f"{label}: formato no válido")
            ctx[key] = s

    return ctx


# ── Resolución de placeholders ───────────────────────────────────────────────────

def resolve(obj, ctx: dict):
    """Sustituye {{key}} con claves de `ctx` (sin eval). Preserva tipos nativos en tokens exactos."""
    if isinstance(obj, dict):
        return {k: resolve(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v, ctx) for v in obj]
    if isinstance(obj, str):
        full = _PLACEHOLDER.fullmatch(obj.strip())
        if full:
            key = full.group(1)
            if key not in ctx:
                raise ValueError(f"Placeholder no resuelto: {{{{{key}}}}}")
            return ctx[key]

        def _repl(m):
            key = m.group(1)
            if key not in ctx:
                raise ValueError(f"Placeholder no resuelto: {{{{{key}}}}}")
            return str(ctx[key])

        return _PLACEHOLDER.sub(_repl, obj)
    return obj
