variable "api_token" {
  type        = string
  description = "Token de API de Proxmox"
  sensitive   = true
}

variable "node_name" {
  type        = string
  description = "El nodo físico de Proxmox donde vivirá la máquina (ej: blade1, blade2)"
}

variable "vm_id" {
  type        = number
  description = "ID único para la nueva máquina virtual (ej: 201)"
}

variable "template_id" {
  type        = number
  description = "ID de la plantilla a clonar. 0 = sin plantilla (adopción de VM existente)."
  default     = 0
}

variable "vm_name" {
  type        = string
  description = "Nombre de la máquina virtual"
}

variable "vm_ip" {
  type        = string
  description = "IP fija en formato CIDR (ej: 192.168.10.55/24) o la palabra 'dhcp'"
  default     = "dhcp"
}

variable "vm_gateway" {
  type        = string
  description = "Puerta de enlace. Dejar vacío si se usa DHCP"
  default     = ""
}

variable "vm_dns" {
  type        = list(string)
  description = "Lista de servidores DNS (ej: ['8.8.8.8', '8.8.4.4'])"
}

variable "vm_dns_domain" {
  type        = string
  description = "Dominio de búsqueda DNS (search domain). Vacío = ninguno."
  default     = ""
}

variable "vm_ram" {
  type        = number
  description = "Memoria RAM en Megabytes"
  default     = 2048
}

variable "vm_cpu" {
  type        = number
  description = "Número de cores de CPU"
  default     = 2
}

variable "disk_size" {
  type        = number
  description = "Tamaño del disco en GB"
  default     = 20
}

variable "ssh_keys" {
  type        = string
  description = "Claves SSH públicas"
  default     = ""
}

variable "network_bridge" {
  type        = string
  description = "El Bridge o VNet donde se conectará la interfaz de red (ej: vmbr0, vnet1, tmpvnet)"
}

variable "admin_user" {
  type        = string
  description = "Usuario administrador por defecto"
  default     = "root"
}

variable "admin_password" {
  type        = string
  description = "Contraseña del usuario administrador"
  sensitive   = true
}

variable "vm_pool" {
  type        = string
  description = "ID del Resource Pool de Proxmox al que pertenecerá la VM (p.ej. 'idp-daniel-pve'). Vacío = sin pool."
  default     = ""
}

variable "app_snippet_id" {
  type        = string
  description = "Volid del snippet vendor-data de la 1-Click App (ej. 'local:snippets/idp-app-docker.yaml'). Vacío = sin app."
  default     = ""
}
