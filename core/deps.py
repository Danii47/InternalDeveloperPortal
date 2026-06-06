"""
Shared singleton instances of heavy service objects.

Imported by routers; initialised once at process start. Using module-level
singletons keeps the same behaviour as the original api.py globals while
keeping routers import-clean.
"""
from core.config import TERRAFORM_DIR
from services.proxmox_client import ProxmoxIDPClient
from services.terraform_runner import TerraformRunner

try:
    pve_client = ProxmoxIDPClient()
    tf_runner = TerraformRunner(tf_directory=TERRAFORM_DIR)
except Exception as exc:
    raise RuntimeError(f"Failed to initialise backend services: {exc}") from exc
