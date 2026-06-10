"""
Manifiesto DECLARATIVO de las acciones que un Blueprint puede componer.

Es la fuente de verdad del conjunto CERRADO de acciones: el constructor de la UI renderiza los
params desde aquí, el loader valida los steps contra este esquema, y el orquestador implementa
exactamente estas acciones (STEP_REGISTRY). Mantener este módulo SIN imports pesados para evitar
ciclos (lo importan loader, router y orchestrator).

Cada param: {key, label, type, required?, secret?, multi?, templatable?, default?, options?, help?}.
`type` (controles del builder): string · text · int · select · cidr · node_select ·
template_select · bridge_multi · tool_multi · vm_name · secret · bool.
`templatable` (def. True para strings): admite `{{variable}}`. `multi`: valor lista (coma→lista).
"""

ACTION_SCHEMA = {
    "create_group": {
        "label": "Crear Grupo",
        "description": "Crea un grupo de Proxmox (para asignarle pools/VNets de un inquilino).",
        "compensable": True,
        "params": [
            {"key": "groupid", "label": "ID del Grupo", "type": "string", "required": True,
             "help": "Ej. g-{{tenant}}"},
            {"key": "comment", "label": "Comentario", "type": "string", "required": False},
        ],
    },
    "create_pool": {
        "label": "Crear Pool",
        "description": "Crea un Resource Pool (idempotente) y opcionalmente fija su cuota de vCPU.",
        "compensable": True,
        "params": [
            {"key": "poolid", "label": "ID del Pool", "type": "string", "required": True,
             "help": "Ej. idp-{{env_name}}"},
            {"key": "comment", "label": "Comentario", "type": "string", "required": False,
             "help": "Texto descriptivo del pool (opcional)."},
            {"key": "group", "label": "Grupo con acceso (opcional)", "type": "string", "required": False,
             "help": "Concede al grupo el rol PVEVMAdmin sobre el pool (acceso del inquilino)."},
            {"key": "quota_cpu", "label": "Cuota vCPU", "type": "int", "required": False,
             "min": 1, "max": 256, "step": 1, "default": 32,
             "help": "Tope total de vCPU sumando todas las VMs del pool."},
            {"key": "quota_ram_gb", "label": "Cuota RAM (GB)", "type": "int", "required": False,
             "min": 1, "max": 512, "step": 1, "default": 32,
             "help": "Tope total de RAM (en GB) del pool."},
            # Legacy en MB (oculto): se mantiene para blueprints antiguos; el editor usa GB.
            {"key": "quota_ram_mb", "type": "int", "required": False, "hidden": True},
            {"key": "quota_disk_gb", "label": "Cuota disco (GB)", "type": "int", "required": False,
             "min": 10, "max": 20480, "step": 10, "default": 500,
             "help": "Tope total de disco (en GB) del pool."},
        ],
    },
    "create_vnet": {
        "label": "Crear VNet (SDN)",
        "description": "Crea una VNet en una zona SDN y su subred. Aplica la configuración SDN.",
        "compensable": True,
        "params": [
            {"key": "vnet", "label": "ID de la VNet (≤8 chars)", "type": "string", "required": True,
             "help": "Identificador corto que Proxmox usa como id de la VNet: 1-8 letras/números, empezando por letra. Ej. {{vnet_id}}"},
            {"key": "alias", "label": "Nombre (alias)", "type": "string", "required": False,
             "help": "Nombre descriptivo y legible de la VNet (alias en Proxmox). Opcional, no tiene límite de 8 caracteres."},
            {"key": "zone", "label": "Zona SDN", "type": "zone_select", "required": True,
             "help": "Zona SDN existente donde se crea la VNet. Las zonas no se crean desde aquí: elígela del desplegable."},
            {"key": "tag", "label": "Tag / VNI (opcional)", "type": "int", "required": False,
             "help": "VLAN id / VNI. Requerido por zonas vlan/vxlan/qinq; se autoasigna uno libre si lo dejas vacío."},
            {"key": "subnet", "label": "Subred (CIDR, opcional)", "type": "cidr", "required": False,
             "help": "Opcional. Déjalo vacío para una VNet sin subred."},
            {"key": "group", "label": "Grupo con acceso (opcional)", "type": "string", "required": False,
             "help": "Concede PVESDNUser al grupo sobre la VNet"},
        ],
    },
    "deploy_vms": {
        "label": "Desplegar VMs",
        "description": "Despliega una o varias VMs (todas las opciones del provisionamiento + múltiples NICs).",
        "compensable": True,
        "params": [
            {"key": "count", "label": "Nº de instancias", "type": "int", "required": True, "default": 1,
             "help": "Cuántas VMs idénticas crear. Se nombran base-1, base-2, …"},
            {"key": "base_name", "label": "Nombre base", "type": "string", "required": True,
             "help": "Prefijo del nombre. Cada VM será «base-1», «base-2», … (letras, números, punto y guion)."},
            {"key": "node", "label": "Nodo", "type": "node_select", "required": True,
             "help": "Nodo Proxmox donde se crearán las VMs."},
            {"key": "template_id", "label": "Plantilla OS", "type": "template_select", "required": True,
             "depends_on": "node", "help": "Plantilla (template) a clonar. Depende del nodo elegido."},
            {"key": "pool", "label": "Pool", "type": "string", "required": False,
             "help": "Pool de recursos donde agrupar las VMs (opcional)."},
            {"key": "nics", "label": "Interfaces de red", "type": "nic_multi", "required": False,
             "help": "Una o varias interfaces; cada una con su bridge/VNet y su IP (DHCP o estática + gateway)."},
            # Legacy (despliegue single-NIC): se mantienen por compatibilidad; el editor usa 'nics'.
            {"key": "bridges", "label": "Redes (VNets/Bridges) [heredado]", "type": "bridge_multi", "required": False,
             "multi": True, "help": "Heredado. Usa 'Interfaces de red'."},
            {"key": "cpu", "label": "vCPU", "type": "int", "required": False, "default": 2},
            {"key": "ram_gb", "label": "RAM (GB)", "type": "int", "required": False, "default": 2,
             "help": "RAM de cada VM en GB."},
            # Legacy en MB (oculto): se mantiene para blueprints antiguos; el editor usa GB.
            {"key": "ram_mb", "type": "int", "required": False, "hidden": True},
            {"key": "disk_gb", "label": "Disco (GB)", "type": "int", "required": False, "default": 20},
            {"key": "admin_user", "label": "Usuario admin", "type": "string", "required": False, "default": "root"},
            {"key": "admin_password", "label": "Contraseña", "type": "secret", "required": False, "secret": True,
             "help": "Vacío → aleatoria efímera. Usa una variable secret para fijarla."},
            {"key": "ip_mode", "label": "Modo IP", "type": "select", "required": False, "default": "dhcp",
             "options": [{"value": "dhcp", "label": "DHCP"}, {"value": "static", "label": "Estática"}],
             "help": "DHCP: la red asigna la IP. Estática: tú indicas la IP inicial y se autoincrementa."},
            {"key": "ip_start", "label": "IP inicial (CIDR, si estática)", "type": "string", "required": False,
             "help": "Solo IP estática. Primera IP CON máscara (ej. 10.0.0.10/24); para varias VMs se autoincrementa."},
            {"key": "gateway", "label": "Puerta de enlace", "type": "string", "required": False,
             "help": "IP del router de la red (ej. 10.0.0.1). Solo IP estática."},
            {"key": "dns", "label": "DNS (coma)", "type": "string", "required": False,
             "help": "Servidores DNS separados por coma (ej. 1.1.1.1, 8.8.8.8)."},
            {"key": "dns_domain", "label": "Dominio DNS", "type": "string", "required": False,
             "help": "Dominio de búsqueda DNS (ej. midominio.local). Opcional."},
            {"key": "ssh_keys", "label": "Claves SSH públicas", "type": "text", "required": False,
             "help": "Una clave pública por línea. Se inyectan al usuario admin."},
            {"key": "ttl_hours", "label": "TTL (horas, 0=persistente)", "type": "int", "required": False,
             "help": "Autodestrucción tras N horas. 0 o vacío = la VM es persistente."},
            {"key": "app_ids", "label": "Apps a instalar", "type": "tool_multi", "required": False, "multi": True},
        ],
    },
    "install_apps": {
        "label": "Instalar apps (guest-agent)",
        "description": "Instala herramientas dentro de una VM existente vía qemu-guest-agent.",
        "compensable": False,
        "params": [
            {"key": "target_vm", "label": "VM destino (nombre)", "type": "vm_name", "required": True},
            {"key": "app_ids", "label": "Apps", "type": "tool_multi", "required": True, "multi": True},
        ],
    },
    "power_action": {
        "label": "Encender / Apagar VM",
        "description": "Cambia el estado de energía de una VM existente.",
        "compensable": False,
        "params": [
            {"key": "target_vm", "label": "VM destino (nombre)", "type": "vm_name", "required": True},
            {"key": "action", "label": "Acción", "type": "select", "required": True, "default": "start",
             "options": [{"value": "start", "label": "Encender"}, {"value": "stop", "label": "Apagar"}]},
        ],
    },
    "create_snapshot": {
        "label": "Crear Snapshot",
        "description": "Crea un snapshot de una VM existente (punto de restauración).",
        "compensable": True,
        "params": [
            {"key": "target_vm", "label": "VM destino (nombre)", "type": "vm_name", "required": True},
            {"key": "snapname", "label": "Nombre del snapshot", "type": "string", "required": True},
            {"key": "description", "label": "Descripción", "type": "string", "required": False},
        ],
    },
}

ALLOWED_ACTIONS = set(ACTION_SCHEMA)

# Tipos de param que llevan lista (en el builder se introducen separados por coma).
MULTI_TYPES = {"bridge_multi", "tool_multi"}

# Tipos de param que se validan como entero cuando son literales (no {{placeholder}}).
INT_TYPES = {"int", "slider"}
