# Blueprints (recetas del orquestador)

Un **Blueprint** es una receta declarativa que un administrador ejecuta desde `/admin/blueprints`
para crear infraestructura compleja en **fases secuenciales**.

## Dónde viven
- Las definiciones se guardan en la tabla SQLite `blueprints` y se **crean/editan desde el
  constructor visual** de la UI (estilo "rutinas").
- Los `*.yaml` de esta carpeta son **semillas**: en el primer arranque, `seed_builtins()` los
  importa a la BD si no existen (quedan como `source='builtin'`, no editables — se **duplican**
  para personalizarlos). Sirven de ejemplo versionado en git.

## Estructura (equivalente al constructor)
```yaml
id: <slug-unico>
name: "Texto visible"
description: "Qué hace"
on_failure: halt            # halt (def.) | rollback
variables: [ ... ]          # inputs que se piden al EJECUTAR (formulario dinámico)
steps: [ ... ]              # fases (acciones secuenciales)
```

### `variables[]`
| campo | notas |
|---|---|
| `key` | identificador (`[a-zA-Z0-9_]`), usado en placeholders `{{key}}` |
| `label` | etiqueta del formulario |
| `type` | `string` · `text` · `cidr` · `int` · `slider` · `select` · `node_select` · `template_select` · `secret` · `bool` |
| `required` | `true`/`false` (def. `true`) |
| `secret` | `true` → password en el form y **redactado** al persistir el run |
| `pattern` | regex (solo `string`) · `min`/`max`/`step`/`default` (números) · `options` (`select`) · `depends_on` (encadenado, p.ej. `template_select`→`node`) |

**Variable derivada**: `{{vnet_id}}` (nombre corto y estable de VNet derivado de `env_name`;
Proxmox limita el id de VNet a 8 chars). Úsala en `create_vnet` y en `bridges` para que coincidan.

### `steps[]`
| campo | notas |
|---|---|
| `id` | identificador del paso |
| `action` | una del **conjunto cerrado** (ver abajo) |
| `name` | texto del timeline (admite `{{var}}`) |
| `params` | argumentos de la acción (admiten `{{var}}`); validados contra el manifiesto `ACTION_SCHEMA` |

## Acciones disponibles (registro cerrado)
| acción | qué hace | compensación (rollback) |
|---|---|---|
| `create_group` | Crea un grupo de Proxmox (para el modelo de inquilino) | borra el grupo |
| `create_pool` | Crea Resource Pool (+cuotas CPU/RAM/disco; +`group` con ACL `PVEVMAdmin`) | borra el pool |
| `create_vnet` | Crea VNet/SDN (subred **opcional**) y aplica SDN; +`group` con ACL `PVESDNUser` | borra la VNet |
| `deploy_vms` | Despliega N VMs: identidad, IP dhcp/estática, dns/dominio, ssh, cpu/ram/disco, **múltiples NICs** (`bridges`), pool, TTL y apps | destruye las VMs |
| `install_apps` | Instala apps (guest-agent) en una VM **por nombre** | — |
| `power_action` | Enciende/apaga una VM por nombre | — |
| `create_snapshot` | Snapshot de una VM por nombre | borra el snapshot |

- `deploy_vms.bridges` acepta **varias** redes (router/multi-NIC). `admin_password` vacío → aleatoria efímera. `subnet` de la VNet es opcional.
- `install_apps`/`power_action`/`create_snapshot` apuntan a una VM por **nombre** (p.ej. `{{tenant}}-srv-1`).

## Modelo de inquilino (grupo + ACL)
Proxmox NO mete VNets en pools; un "inquilino" se modela con un **grupo** al que se le da acceso a
sus recursos vía ACL:
- `create_group` → `g-{{tenant}}`.
- `create_pool` con `group: g-{{tenant}}` → ACL `PVEVMAdmin` sobre `/pool/<id>`.
- `create_vnet` con `group: g-{{tenant}}` → ACL `PVESDNUser` sobre `/sdn/zones/<zona>/<vnet>`.
- `deploy_vms` con `pool` y `bridges` apuntando a esos recursos.
Todo `group`/`pool` es **opcional**. En el constructor, cada campo de `deploy_vms` (nodo, plantilla,
pool, redes) tiene un conmutador **Fijo/Variable** para enlazar pasos con variables pedidas al lanzar.

## Pre-flight (validación antes de lanzar)
Antes de crear nada, el IDP valida contra la API (read-only) y **bloquea** si hay errores:
- privilegios del token (ver abajo), formato/longitud de nombres (vnet ≤8, pool, grupo, vm),
- colisiones: VM ya existente → error (evita romper al relanzar); pool/vnet/grupo existentes → aviso (se reutilizan),
- existencia de nodo/plantilla/red/pool/grupo **o** que los cree un paso previo (lookahead).

## Permisos del token de Proxmox
El token (`PROXMOX_TOKEN_ID`) necesita privilegios suficientes; lo más simple es rol **`Administrator` en `/`**
(o, fino: `Pool.Allocate`, `SDN.Allocate`/`SDN.Use`, `VM.Allocate`, `Datastore.AllocateSpace`,
`Permissions.Modify` para ACLs de grupo, `User.Modify` para grupos, `VM.GuestAgent.*` para apps).
**Atención:** los tokens de API con *privilege separation* activada NO heredan los permisos del usuario;
desactívala o concédele el rol explícitamente. (Esto causa el típico `403 ... Pool.Allocate`.)

## Reglas y seguridad
- `action` del conjunto cerrado; params validados contra `ACTION_SCHEMA`; placeholders solo a variables declaradas/derivadas.
- Validación **server-side** en crear/editar y **pre-flight** en ejecutar (nunca se confía en el cliente).
- Sustitución `{{}}` literal (sin `eval`); cada handler re-sanea su entrada. Inputs `secret` redactados en el historial.
- Los `builtin` no se editan/borran (403); se duplican a `source='user'`.

## Manejo de errores
- `halt` (def.): se detiene la secuencia y se reporta lo creado (para inspección/limpieza).
- `rollback`: compensación inversa (saga) en orden inverso; si un compensador falla → `rollback_partial`.

## Notas operativas (v1)
- **Idempotencia**: `create_pool`/`create_vnet` comprueban existencia. `deploy_vms` crea VMs nuevas en cada ejecución (re-lanzar añade más).
- **Concurrencia**: solo `deploy_vms` toma el `deployment_lock` (Terraform); el resto de acciones (API pura) corren inline en el hilo del run → no congelan la cola ni el pool.
- **Credenciales**: contraseña efímera si no se fija; usa SSH/`reprovision` o una variable `secret`.

## Verificación E2E (nodo real)
1. Requisitos: SDN habilitado; token admin con permisos `Pool`/`SDN`/`VM`/`VM.GuestAgent`.
2. UI **Blueprints**: crear/duplicar una receta, ejecutar y seguir el timeline (○ pendiente · ● ejecutando · ✓ ok · ✗ fallo).
3. Receta router: 2 VNets + VM multi-NIC + apps → verificar NICs, conectividad y apps.
4. Fallo + HALT: forzar fallo (p.ej. CIDR en uso) → se detiene y reporta lo creado.
5. Fallo + rollback: marcar "Revertir" y forzar fallo en el último paso → compensación inversa (VMs → VNet → Pool) y estado `rolled_back`/`rollback_partial`.
