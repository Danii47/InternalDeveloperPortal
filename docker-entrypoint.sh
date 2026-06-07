#!/bin/bash
set -euo pipefail

APP_USER=idp
QUOTAS_DB_PATH="${QUOTAS_DB_PATH:-/app/data/quotas.db}"
DATA_DIR="$(dirname "$QUOTAS_DB_PATH")"
TF_DIR="${TERRAFORM_DIR:-/app/terraform/vm_base}"

# ── 1. Permisos: si arrancamos como root, preparamos los volúmenes y bajamos a 'idp'
#       con gosu. Si ya somos no-root (p.ej. `user:` en compose), seguimos tal cual. ──
RUN_AS=""
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  # Los bind-mounts pueden venir como root; los hacemos escribibles por el usuario de la app.
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR" 2>/dev/null || true
  [ -d "$TF_DIR" ] && chown -R "$APP_USER:$APP_USER" "$TF_DIR" 2>/dev/null || true
  RUN_AS="gosu $APP_USER"
else
  mkdir -p "$DATA_DIR" 2>/dev/null || true
fi
echo "[IDP] SQLite path: $QUOTAS_DB_PATH"

# ── 2. Inicializar proveedores de Terraform en el primer arranque (como usuario app) ──
if [ -d "$TF_DIR" ]; then
  if [ ! -d "$TF_DIR/.terraform" ]; then
    echo "[IDP] Terraform providers not found — running 'terraform init' in $TF_DIR ..."
    $RUN_AS sh -c "cd '$TF_DIR' && terraform init -no-color -input=false"
    echo "[IDP] Terraform initialised."
  else
    echo "[IDP] Terraform providers already present — skipping init."
  fi
else
  echo "[IDP] WARNING: TERRAFORM_DIR not found at $TF_DIR — Terraform operations will fail."
fi

# ── 3. Arrancar FastAPI (sin privilegios de root) ────────────────────────────────
# workers=1 es intencional: la cola de tareas y la sesión viven en memoria.
echo "[IDP] Starting Uvicorn on 0.0.0.0:8000 ..."
exec $RUN_AS uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --no-access-log
