terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.106"
    }
  }
}

provider "proxmox" {
  endpoint  = "https://192.168.10.10:8006/"
  api_token = var.api_token
  insecure  = true
}
