"""
Catálogo de herramientas instalables (multi-select) vía qemu-guest-agent.

Única fuente de verdad: cada herramienta declara CÓMO se instala por familia de SO
(`debian` | `rhel` | `freebsd`). El usuario solo envía IDs de un conjunto cerrado; los
comandos son predefinidos aquí (cero texto libre → seguro).

Tipos:
  - "package": nombre de paquete por familia → se instala con el gestor (PKG_INSTALL),
    y TODOS los package marcados se agrupan en un único comando (rápido, idempotente).
  - "script": comando(s) explícitos por familia (instaladores oficiales, enable de servicio…).
Un valor None en una familia ⇒ la herramienta no aplica a ese SO (se omite y se reporta).
"""

OS_FAMILIES = ("debian", "rhel", "freebsd")

# Comando base del gestor de paquetes por familia. {pkgs} = lista separada por espacios.
PKG_INSTALL = {
    "debian": "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && apt-get install -y {pkgs}",
    "rhel": "dnf install -y {pkgs}",
    "freebsd": "ASSUME_ALWAYS_YES=yes pkg install -y {pkgs}",
}

# Mapeo del id de SO (qemu-guest-agent get-osinfo) → familia soportada.
_OS_ID_TO_FAMILY = {
    "debian": "debian",
    "ubuntu": "debian",
    "linuxmint": "debian",
    "pop": "debian",
    "raspbian": "debian",
    "devuan": "debian",
    "rhel": "rhel",
    "centos": "rhel",
    "rocky": "rhel",
    "almalinux": "rhel",
    "fedora": "rhel",
    "ol": "rhel",
    "oraclelinux": "rhel",
    "amzn": "rhel",
    "freebsd": "freebsd",
}


def os_family_from_id(os_id: str) -> str | None:
    """Normaliza el `id` de get-osinfo a una familia soportada, o None si no se soporta."""
    return _OS_ID_TO_FAMILY.get((os_id or "").strip().lower())


# Helpers para construir entradas de servicio (instalar + habilitar) por familia.
def _svc_debian(pkg: str, svc: str) -> str:
    return f"{PKG_INSTALL['debian'].format(pkgs=pkg)} && systemctl enable --now {svc}"


def _svc_rhel(pkg: str, svc: str) -> str:
    return f"{PKG_INSTALL['rhel'].format(pkgs=pkg)} && systemctl enable --now {svc}"


def _svc_freebsd(pkg: str, rcvar: str, svc: str) -> str:
    return f"{PKG_INSTALL['freebsd'].format(pkgs=pkg)} && sysrc {rcvar}=YES && service {svc} start"


_DOCKER = "curl -fsSL https://get.docker.com | sh && systemctl enable --now docker"
_K3S = "curl -sfL https://get.k3s.io | sh -"


# ── Registro ────────────────────────────────────────────────────────────────────
# Cada entrada: id, name, icon (svg en /apps/<icon>.svg), category, type, y pkg|script.
TOOLS = [
    # ── CLI esenciales (package, se agrupan en un solo comando) ──
    {
        "id": "git",
        "name": "Git",
        "icon": "git",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "git", "rhel": "git", "freebsd": "git"},
    },
    {
        "id": "curl",
        "name": "curl",
        "icon": "curl",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "curl", "rhel": "curl", "freebsd": "curl"},
    },
    {
        "id": "wget",
        "name": "wget",
        "icon": "wget",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "wget", "rhel": "wget", "freebsd": "wget"},
    },
    {
        "id": "vim",
        "name": "Vim",
        "icon": "vim",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "vim", "rhel": "vim-enhanced", "freebsd": "vim"},
    },
    {
        "id": "htop",
        "name": "htop",
        "icon": "htop",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "htop", "rhel": "htop", "freebsd": "htop"},
    },
    {
        "id": "tmux",
        "name": "tmux",
        "icon": "tmux",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "tmux", "rhel": "tmux", "freebsd": "tmux"},
    },
    {
        "id": "jq",
        "name": "jq",
        "icon": "jq",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "jq", "rhel": "jq", "freebsd": "jq"},
    },
    {
        "id": "unzip",
        "name": "unzip",
        "icon": "unzip",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "unzip", "rhel": "unzip", "freebsd": "unzip"},
    },
    {
        "id": "net-tools",
        "name": "net-tools",
        "icon": "nettools",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "net-tools", "rhel": "net-tools", "freebsd": "net-tools"},
    },
    {
        "id": "dnsutils",
        "name": "DNS utils",
        "icon": "dns",
        "category": "CLI",
        "type": "package",
        "pkg": {"debian": "dnsutils", "rhel": "bind-utils", "freebsd": "bind-tools"},
    },
    {
        "id": "bash-completion",
        "name": "bash-completion",
        "icon": "bash",
        "category": "CLI",
        "type": "package",
        "pkg": {
            "debian": "bash-completion",
            "rhel": "bash-completion",
            "freebsd": "bash-completion",
        },
    },
    # ── Runtimes / contenedores ──
    {
        "id": "docker",
        "name": "Docker CE",
        "icon": "docker",
        "category": "Runtimes",
        "type": "script",
        "script": {"debian": _DOCKER, "rhel": _DOCKER, "freebsd": None},
    },
    {
        "id": "podman",
        "name": "Podman",
        "icon": "podman",
        "category": "Runtimes",
        "type": "package",
        "pkg": {"debian": "podman", "rhel": "podman", "freebsd": "podman"},
    },
    {
        "id": "node",
        "name": "Node.js",
        "icon": "nodejs",
        "category": "Runtimes",
        "type": "package",
        "pkg": {"debian": "nodejs npm", "rhel": "nodejs npm", "freebsd": "node npm"},
    },
    {
        "id": "python",
        "name": "Python 3",
        "icon": "python",
        "category": "Runtimes",
        "type": "package",
        "pkg": {
            "debian": "python3 python3-pip",
            "rhel": "python3 python3-pip",
            "freebsd": "python3",
        },
    },
    {
        "id": "java",
        "name": "Java (OpenJDK)",
        "icon": "java",
        "category": "Runtimes",
        "type": "package",
        "pkg": {
            "debian": "default-jdk",
            "rhel": "java-latest-openjdk",
            "freebsd": "openjdk",
        },
    },
    # ── Orquestación / infra ──
    {
        "id": "k3s",
        "name": "Kubernetes (k3s)",
        "icon": "kubernetes",
        "category": "Infra",
        "type": "script",
        "script": {"debian": _K3S, "rhel": _K3S, "freebsd": None},
    },
    {
        "id": "nginx",
        "name": "Nginx",
        "icon": "nginx",
        "category": "Infra",
        "type": "script",
        "script": {
            "debian": _svc_debian("nginx", "nginx"),
            "rhel": _svc_rhel("nginx", "nginx"),
            "freebsd": _svc_freebsd("nginx", "nginx_enable", "nginx"),
        },
    },
    {
        "id": "caddy",
        "name": "Caddy",
        "icon": "caddy",
        "category": "Infra",
        "type": "script",
        "script": {
            "debian": (
                "export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && "
                "apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg && "
                "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && "
                "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list && "
                "apt-get update -qq && apt-get install -y caddy && systemctl enable --now caddy"
            ),
            "rhel": (
                "dnf install -y 'dnf-command(copr)' && dnf copr enable -y @caddy/caddy && "
                "dnf install -y caddy && systemctl enable --now caddy"
            ),
            "freebsd": _svc_freebsd("caddy", "caddy_enable", "caddy"),
        },
    },
    # ── Bases de datos ──
    {
        "id": "postgresql",
        "name": "PostgreSQL",
        "icon": "postgresql",
        "category": "Bases de datos",
        "type": "script",
        "script": {
            "debian": _svc_debian("postgresql", "postgresql"),
            "rhel": (
                "dnf install -y postgresql-server && postgresql-setup --initdb && "
                "systemctl enable --now postgresql"
            ),
            "freebsd": (
                "ASSUME_ALWAYS_YES=yes pkg install -y postgresql16-server && "
                "sysrc postgresql_enable=YES && service postgresql initdb && service postgresql start"
            ),
        },
    },
    {
        "id": "mariadb",
        "name": "MariaDB",
        "icon": "mariadb",
        "category": "Bases de datos",
        "type": "script",
        "script": {
            "debian": _svc_debian("mariadb-server", "mariadb"),
            "rhel": _svc_rhel("mariadb-server", "mariadb"),
            "freebsd": _svc_freebsd(
                "mariadb106-server", "mysql_enable", "mysql-server"
            ),
        },
    },
    {
        "id": "redis",
        "name": "Redis",
        "icon": "redis",
        "category": "Bases de datos",
        "type": "script",
        "script": {
            "debian": _svc_debian("redis-server", "redis-server"),
            "rhel": _svc_rhel("redis", "redis"),
            "freebsd": _svc_freebsd("redis", "redis_enable", "redis"),
        },
    },
]

_BY_ID = {t["id"]: t for t in TOOLS}
_IDS = set(_BY_ID)
_CATEGORY_ORDER = ["CLI", "Runtimes", "Infra", "Bases de datos"]


def is_valid(tool_id: str) -> bool:
    return tool_id in _IDS


def list_tools() -> list:
    """Catálogo agrupado por categoría (metadatos para el frontend, sin comandos)."""
    groups = {c: [] for c in _CATEGORY_ORDER}
    for t in TOOLS:
        groups.setdefault(t["category"], []).append(
            {"id": t["id"], "name": t["name"], "icon": t["icon"]}
        )
    return [
        {"category": c, "tools": groups[c]} for c in _CATEGORY_ORDER if groups.get(c)
    ]


def get_install_plan(tool_ids: list, family: str) -> list:
    """
    Devuelve los pasos de instalación ordenados para `tool_ids` en una `family`:
      [{"tool": <id|"__packages__">, "name": str, "command": str|None}]
    - Todos los `package` válidos se agrupan en UN solo paso (gestor del SO).
    - Cada `script` es un paso propio.
    - `command=None` ⇒ herramienta no soportada en esa familia (el instalador la omite).
    El orden conserva el del catálogo (CLI → Runtimes → Infra → BBDD).
    """
    selected = [_BY_ID[i] for i in tool_ids if i in _BY_ID]
    selected.sort(key=lambda t: TOOLS.index(t))

    plan: list = []
    pkg_names: list = []
    pkg_labels: list = []

    for t in selected:
        if t["type"] == "package":
            name = t["pkg"].get(family)
            if name:
                pkg_names.extend(name.split())
                pkg_labels.append(t["name"])
            else:
                plan.append({"tool": t["id"], "name": t["name"], "command": None})
        else:  # script
            cmd = t["script"].get(family)
            plan.append({"tool": t["id"], "name": t["name"], "command": cmd})

    if pkg_names:
        combined = PKG_INSTALL[family].format(pkgs=" ".join(pkg_names))
        plan.insert(
            0,
            {
                "tool": "__packages__",
                "name": "Paquetes: " + ", ".join(pkg_labels),
                "command": combined,
            },
        )
    return plan
