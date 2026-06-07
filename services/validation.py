"""
Reglas de validación de nombres de recursos Proxmox, en un único sitio.

Usadas por el orquestador (defensa en runtime) y por el pre-flight (fallar antes de lanzar).
Cada `validate_*` devuelve un mensaje de error (str) o None si es válido.
"""
import ipaddress
import re

# VNet y zona SDN: Proxmox limita el id a 8 caracteres alfanuméricos, empezando por letra.
VNET_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")
ZONE_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")
# Pool y grupo: alfanumérico + . _ - (Proxmox), longitud razonable.
POOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# Nombre de VM (mismo criterio que el despliegue normal).
VM_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{0,62}$")

_RULES = {
    "vnet": (VNET_RE, "1-8 caracteres alfanuméricos, empezando por letra minúscula"),
    "zone": (ZONE_RE, "1-8 caracteres alfanuméricos, empezando por letra minúscula"),
    "pool": (POOL_RE, "letras, números y . _ - (máx. 63), empezando por alfanumérico"),
    "group": (GROUP_RE, "letras, números y . _ - (máx. 63), empezando por alfanumérico"),
    "vm": (VM_NAME_RE, "letras, números, puntos y guiones (máx. 63), empezando por alfanumérico"),
}


def validate_name(kind: str, value: str) -> str | None:
    """Valida `value` según `kind` (vnet|zone|pool|group|vm). Devuelve error o None."""
    rule = _RULES.get(kind)
    if rule is None:
        return None
    regex, hint = rule
    if not value or not regex.match(str(value)):
        return f"Nombre inválido para {kind} '{value}': {hint}"
    return None


def validate_cidr(value: str) -> str | None:
    """Valida un CIDR IPv4/IPv6. Devuelve error o None."""
    try:
        ipaddress.ip_network(str(value), strict=False)
        return None
    except ValueError:
        return f"CIDR inválido: '{value}' (ej. 10.20.0.1/24)"


def validate_ip(value: str) -> str | None:
    """Valida una IP de host (sin máscara), p.ej. una puerta de enlace o un DNS. Error o None."""
    try:
        ipaddress.ip_address(str(value).strip())
        return None
    except ValueError:
        return f"IP inválida: '{value}' (ej. 192.168.1.1)"


def validate_static_cidr(value: str) -> str | None:
    """
    Valida una IP estática en formato CIDR con máscara de red utilizable (p.ej. 10.0.0.10/24).
    Rechaza la IP sin máscara y un prefijo de host único (/32 IPv4, /128 IPv6), que dejaría a la
    VM sin poder hablar con su puerta de enlace. Devuelve error o None.
    """
    s = str(value).strip()
    if "/" not in s:
        return f"La IP estática debe incluir la máscara en formato CIDR: '{value}' (ej. 10.0.0.10/24)."
    try:
        iface = ipaddress.ip_interface(s)
    except ValueError:
        return f"CIDR inválido: '{value}' (ej. 10.0.0.10/24)."
    if iface.network.prefixlen >= iface.network.max_prefixlen:
        return (f"Prefijo demasiado estrecho en '{value}': usa una máscara de red real "
                f"(ej. /24), no /{iface.network.max_prefixlen}.")
    return None
