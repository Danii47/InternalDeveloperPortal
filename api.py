# Backward-compatibility shim.
# The application entry point is now main.py.
# `uvicorn api:app` continues to work unchanged.
from main import app  # noqa: F401
