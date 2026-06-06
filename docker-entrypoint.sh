#!/bin/bash
set -euo pipefail

# ── 1. Ensure the SQLite data directory exists ────────────────────────────────
QUOTAS_DB_PATH="${QUOTAS_DB_PATH:-/app/data/quotas.db}"
DATA_DIR="$(dirname "$QUOTAS_DB_PATH")"
mkdir -p "$DATA_DIR"
echo "[IDP] SQLite path: $QUOTAS_DB_PATH"

# ── 2. Initialise Terraform providers on first run ────────────────────────────
TF_DIR="${TERRAFORM_DIR:-/app/terraform/vm_base}"

if [ -d "$TF_DIR" ]; then
  if [ ! -d "$TF_DIR/.terraform" ]; then
    echo "[IDP] Terraform providers not found — running 'terraform init' in $TF_DIR ..."
    ( cd "$TF_DIR" && terraform init -no-color -input=false )
    echo "[IDP] Terraform initialised."
  else
    echo "[IDP] Terraform providers already present — skipping init."
  fi
else
  echo "[IDP] WARNING: TERRAFORM_DIR not found at $TF_DIR — Terraform operations will fail."
fi

# ── 3. Start the FastAPI application ─────────────────────────────────────────
# workers=1 is intentional: the task queue and session store are in-memory.
# Scaling requires migrating to an external queue/store first.
echo "[IDP] Starting Uvicorn on 0.0.0.0:8000 ..."
exec uvicorn api:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --no-access-log
