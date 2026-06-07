from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import database
from core.config import ALLOWED_ORIGINS
from routers import admin, auth, blueprints, console, deploy, inventory

app = FastAPI(title="Proxmox IDP API", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

database.init_db()

# Sembrar los blueprints builtin (YAML del repo) en BD si aún no existen (idempotente).
from services import blueprint_loader  # noqa: E402
blueprint_loader.seed_builtins()

# Import AFTER init_db() so the vm_lifecycles table exists before the first sweep.
# Importing the module starts the TTL reaper daemon thread (when REAPER_ENABLED).
from services import reaper  # noqa: E402,F401

app.include_router(auth.router)
app.include_router(inventory.router)
app.include_router(deploy.router)
app.include_router(admin.router)
app.include_router(console.router)
app.include_router(blueprints.router)
