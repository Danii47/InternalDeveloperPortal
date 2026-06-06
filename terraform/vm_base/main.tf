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

  network_device {
    bridge = var.network_bridge
  }

  initialization {
    datastore_id = "local"

    # Vendor-data de la 1-Click App (canal independiente del user_account de identidad).
    # Vacío → null → cloud-init de identidad intacto (comportamiento base sin app).
    vendor_data_file_id = var.app_snippet_id != "" ? var.app_snippet_id : null

    ip_config {
      ipv4 {
        address = var.vm_ip
        gateway = var.vm_ip == "dhcp" ? null : var.vm_gateway
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
