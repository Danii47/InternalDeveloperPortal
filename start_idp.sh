#!/bin/bash

echo "🚀 Iniciando Proxmox IDP..."

# 1. Activar el entorno virtual
source /opt/internal-developer-platform/venv/bin/activate

# 2. Matar procesos anteriores (por si se quedaron colgados)
pkill -f "uvicorn main:app"
pkill -f "astro dev"

# 3. Arrancar la API de Python en segundo plano
echo "📡 Levantando Backend (FastAPI) en puerto 8000..."
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > backend.log 2>&1 &

# 4. Arrancar el Frontend de Astro en segundo plano
echo "🎨 Levantando Frontend (Astro) en puerto 4321..."
cd /opt/internal-developer-platform/frontend
nohup pnpm run dev -- --host > frontend.log 2>&1 &

echo "✅ ¡Todo el sistema está online!"
echo "👉 Panel Web: http://<TU_IP>:4321"
echo "👉 Documentación API: http://<TU_IP>:8000/docs"
