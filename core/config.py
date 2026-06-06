import os
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

# ── Proxmox ──────────────────────────────────────────────────────────────────
PROXMOX_URL: str = os.getenv("PROXMOX_URL", "")
PROXMOX_TOKEN_ID: str = os.getenv("PROXMOX_TOKEN_ID", "")
PROXMOX_SECRET_TOKEN: str = os.getenv("PROXMOX_SECRET_TOKEN", "")

_pve = urlparse(PROXMOX_URL if PROXMOX_URL else "https://localhost:8006")
PROXMOX_HOST: str = _pve.hostname or "localhost"
PROXMOX_PORT: int = _pve.port or 8006

# ── Auth / JWT ────────────────────────────────────────────────────────────────
SECRET_KEY: str = os.getenv("IDP_SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("IDP_SECRET_KEY no está configurado en .env")

SESSION_TTL: int = 2 * 3600   # 2 h — mirrors Proxmox ticket TTL
JWT_EXPIRY_H: int = 8          # hard upper bound for issued JWTs
SECURE_COOKIE: bool = os.getenv("IDP_SECURE_COOKIE", "false").lower() == "true"

# ── CORS — comma-separated list supported (e.g. "http://host,http://host:4321") ──
ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("IDP_ALLOWED_ORIGIN", "http://localhost:4321").split(",")
    if o.strip()
]

# ── Terraform ─────────────────────────────────────────────────────────────────
TERRAFORM_DIR: str = os.getenv(
    "TERRAFORM_DIR",
    "/opt/internal-developer-platform/terraform/vm_base",
)

# ── Ephemeral environments / TTL reaper ─────────────────────────────────────────
# REAPER_ENABLED   — master switch; set false to disable auto-destruction entirely.
# REAPER_INTERVAL  — seconds between sweeps (default 1 h).
# REAPER_GRACE_HOURS — grace window after a VM's TTL elapses before it is destroyed.
# TTL_MAX_HOURS    — upper bound for any requested/renewed TTL (default 365 days).
REAPER_ENABLED: bool = os.getenv("IDP_REAPER_ENABLED", "true").lower() == "true"
REAPER_INTERVAL: int = int(os.getenv("IDP_REAPER_INTERVAL", "3600"))
REAPER_GRACE_HOURS: float = float(os.getenv("IDP_REAPER_GRACE_HOURS", "1"))
TTL_MAX_HOURS: int = int(os.getenv("IDP_TTL_MAX_HOURS", str(365 * 24)))
