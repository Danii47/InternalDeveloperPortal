# Proxmox IDP — Internal Developer Platform

Plataforma self-service para clústeres **Proxmox VE**: permite a los equipos desplegar y gestionar
máquinas virtuales mediante **Terraform**, con autenticación contra Proxmox, RBAC por *impersonation*,
cuotas por pool, entornos efímeros, catálogo de aplicaciones y un orquestador de *blueprints*.

> Comunicación **100% API HTTP** con Proxmox (sin SSH). Pensado para ser *plug & play* y open source.

## Características
- **Provisionamiento** de VMs desde plantillas (clonado vía Terraform), con CPU/RAM/disco, red
  (DHCP/estática, DNS, dominio), claves SSH y multi-NIC.
- **Autenticación y RBAC**: login contra Proxmox (realms `pve`/`pam`), JWT en cookie HttpOnly y
  *impersonation* por ticket → cada usuario ve/gestiona solo lo suyo. Pool propio por usuario.
- **Cuotas** por pool (CPU/RAM/disco) con panel de administración.
- **Entornos efímeros (TTL)**: autodestrucción programada con ventana de gracia (*reaper*).
- **Catálogo de aplicaciones**: instalación de herramientas en el primer arranque vía
  *qemu-guest-agent*, según el SO (Debian/Ubuntu, RHEL/Fedora, FreeBSD).
- **Blueprints**: orquestador secuencial (crear pool, VNet/SDN, desplegar VMs, instalar apps,
  power, snapshots) con constructor visual, validación y rollback (saga). Solo administradores.

## Arquitectura
| Capa | Tecnología |
|---|---|
| Frontend | Astro (SSR) + Tailwind |
| Backend | FastAPI (Python 3.13) |
| IaC | Terraform + provider `bpg/proxmox` |
| Estado | SQLite (cuotas, ciclos de vida, blueprints, runs) |

```
frontend/         UI Astro (páginas, componentes, middleware de sesión)
core/             config, seguridad (JWT/RBAC), dependencias singleton
routers/          endpoints FastAPI (auth, inventory, deploy, admin, console, blueprints)
services/         lógica: proxmox_client, terraform_runner, task_manager, reaper,
                  tool_catalog, app_installer, actions, blueprint_loader, orchestrator
terraform/vm_base Configuración Terraform de la VM
blueprints/       Recetas semilla (YAML) para el orquestador
```

## Puesta en marcha (Docker)
1. Requisitos en Proxmox: un **token de API** (`usuario@realm!nombre`) con permisos para gestionar
   VMs/pools/SDN, y el content type *Snippets* / SDN habilitados si se usan blueprints.
2. Copia y rellena la configuración:
   ```bash
   cp .env.example .env
   # edita PROXMOX_URL, PROXMOX_TOKEN_ID, PROXMOX_SECRET_TOKEN, IDP_SECRET_KEY, IDP_ALLOWED_ORIGIN, PUBLIC_API_URL
   ```
3. Arranca:
   ```bash
   docker compose up -d --build
   ```
   - Panel: `http://<host>` (puerto 80) · API/Docs: `http://<host>:8000/docs`

### Desarrollo local
- Backend: `uvicorn main:app --reload` (con el venv y `.env`).
- Frontend: `cd frontend && pnpm install && pnpm dev`.

## Configuración (`.env`)
Ver `.env.example`. Variables clave: `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_SECRET_TOKEN`,
`IDP_SECRET_KEY` (genera una larga y aleatoria), `IDP_SECURE_COOKIE` (true tras HTTPS),
`IDP_ALLOWED_ORIGIN` (CORS), `PUBLIC_API_URL` (URL de la API para el navegador). El endpoint de
Proxmox para Terraform se inyecta desde `PROXMOX_URL` (no hay valores hardcodeados).

## Notas de seguridad
- Las credenciales viven en `.env` (gitignored); **nunca** se commitean. La BD SQLite (`*.db`,
  `data/`) y el estado de Terraform están en `.gitignore`.
- Contraseñas, tokens y claves se pasan a Terraform por **entorno** (no por argv) y se redactan en logs.
- Las acciones de Blueprints son un **conjunto cerrado** vetado; los inputs se validan en backend
  y la sustitución `{{var}}` es literal (sin `eval`). El pre-flight valida nombres/colisiones contra
  la API (bloquea ante errores reales) y **avisa** si el token podría no tener privilegios (la
  autoridad final es Proxmox al ejecutar; un sondeo no fiable nunca debe impedir un lanzamiento legítimo).
- **Login con rate-limit** anti fuerza-bruta (por IP). **TLS verificable** hacia Proxmox vía
  `IDP_PROXMOX_VERIFY`/`IDP_PROXMOX_CA` (por defecto inseguro para certs autofirmados; actívalo en producción).
- El contenedor **ejecuta la app como usuario no-root** (`idp`); root solo ajusta permisos al arrancar (`gosu`).
- En producción: sirve por **HTTPS** con `IDP_SECURE_COOKIE=true`, usa un **token de mínimo privilegio**
  y no expongas el panel a Internet. `--workers 1` es intencional (cola/sesión en memoria).

## Licencia
Pendiente de definir por el mantenedor (añade un `LICENSE` antes de publicar).
