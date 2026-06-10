locals {
  # NICs: cada elemento = una interfaz de red con su config IP propia. Se usa network_nics si
  # viene; si no, se deriva de las variables legacy (network_bridges/network_bridge + vm_ip/vm_gateway)
  # para mantener intacto el despliegue normal y adopt/destroy.
  nics = length(var.network_nics) > 0 ? var.network_nics : [
    for b in (length(var.network_bridges) > 0 ? var.network_bridges : [var.network_bridge]) :
    { bridge = b, ip = var.vm_ip, gateway = var.vm_gateway }
  ]
}

resource "proxmox_virtual_environment_vm" "server" {
  vm_id     = var.vm_id
  name      = var.vm_name
  node_name = var.node_name
  pool_id   = var.vm_pool != "" ? var.vm_pool : null

  tags = ["managed-by-idp"]

  # template_id = 0 → VM adoptada sin plantilla de origen (bloque omitido).
  # template_id > 0 → VM clonada desde una plantilla (comportamiento normal).
  dynamic "clone" {
    for_each = var.template_id > 0 ? [1] : []
    content {
      vm_id = var.template_id
      full  = true
    }
  }

  disk {
    datastore_id = "local"
    interface    = "scsi0"
    size         = var.disk_size
  }

  agent {
    enabled = true
  }

  cpu {
    cores = var.vm_cpu
  }

  memory {
    dedicated = var.vm_ram
  }

  dynamic "network_device" {
    for_each = local.nics
    content {
      bridge = network_device.value.bridge
    }
  }

  initialization {
    datastore_id = "local"

    # Vendor-data de la 1-Click App (canal independiente del user_account de identidad).
    # Vacío → null → cloud-init de identidad intacto (comportamiento base sin app).
    vendor_data_file_id = var.app_snippet_id != "" ? var.app_snippet_id : null

    # Un ip_config por NIC, en el MISMO orden que network_device (Proxmox ipconfig0..N).
    dynamic "ip_config" {
      for_each = local.nics
      content {
        ipv4 {
          address = ip_config.value.ip
          gateway = (ip_config.value.ip == "dhcp" || ip_config.value.gateway == "") ? null : ip_config.value.gateway
        }
      }
    }

    dns {
      # Se aplican los DNS indicados en cualquier modo (también DHCP); vacío → null.
      domain  = var.vm_dns_domain != "" ? var.vm_dns_domain : null
      servers = length(var.vm_dns) > 0 ? var.vm_dns : null
    }

    user_account {
      username = var.admin_user
      password = var.admin_password
      keys     = split("\n", trimspace(var.ssh_keys))
    }
  }
}

output "vm_ip_assigment" {
  value       = proxmox_virtual_environment_vm.server.ipv4_addresses
  description = "Las IPs asignadas a la máquina virtual"
}

output "vm_id" {
  value       = proxmox_virtual_environment_vm.server.vm_id
  description = "El ID de la máquina virtual creada"
}
