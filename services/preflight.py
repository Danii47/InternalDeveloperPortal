"""
Pre-flight de Blueprints: valida una ejecución ANTES de crear nada (fail-fast).

Recorre los pasos en orden manteniendo un conjunto de "recursos planificados" (lo que crearán
los pasos previos), de modo que una VNet creada en el paso 1 se considera válida como red en el
paso 4. Comprueba, solo-lectura contra la API de Proxmox:
  - permisos del token (ataja el 403 antes de ejecutar),
  - formato/longitud de nombres (vnet ≤8, pool, grupo, vm),
  - colisiones (VM ya existente → error; pool/vnet/grupo existentes → aviso, se reutilizan),
  - existencia de nodo/plantilla/redes/pool/grupo (o que se creen en un paso previo).

Devuelve {"errors": [...], "warnings": [...]}. Con errores, el run se aborta.
"""
import os

from core.deps import pve_client
from services import blueprint_loader, orchestrator, validation

def _opt(v):
    v = str(v).strip() if v is not None else ""
    return v or None


def _intval(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _aslist(val) -> list:
    """Normaliza a lista de strings (acepta lista o cadena separada por comas)."""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if val is None or str(val).strip() == "":
        return []
    return [s.strip() for s in str(val).split(",") if s.strip()]


def _is_tpl(v) -> bool:
    """True si el valor contiene un placeholder {{var}} sin resolver (no validable como literal)."""
    return "{{" in str(v)


def _eff_priv(perms: dict, priv: str, path: str) -> bool:
    """
    True si el token tiene `priv` EFECTIVO en `path`. Proxmox (`/access/permissions`) devuelve el
    conjunto acumulado por ruta, así que consultamos la entrada del ancestro MÁS específico que
    exista en el mapa (un permiso en '/' no siempre propaga a subárboles como /sdn → hay que mirar
    la ruta real, no 'cualquier ruta').
    """
    parts = [seg for seg in path.split("/") if seg]
    candidates = ["/"] + ["/" + "/".join(parts[:i + 1]) for i in range(len(parts))]
    for cand in reversed(candidates):  # del más específico al más general
        if cand in perms:
            return bool(perms[cand].get(priv))
    return False


def _perm_reqs(action: str, p: dict) -> list:
    """Privilegios (priv, ruta) que un paso necesita de verdad, en la ruta donde se comprueban."""
    if action == "create_group":
        return [("User.Modify", "/access/groups")]
    if action == "create_pool":
        reqs = [("Pool.Allocate", "/pool")]
        if _opt(p.get("group")):
            reqs.append(("Permissions.Modify", "/pool"))  # conceder ACL de grupo sobre el pool
        return reqs
    if action == "create_vnet":
        zone = _opt(p.get("zone")) or "idpzone"
        reqs = [("SDN.Allocate", "/sdn")]
        if _opt(p.get("group")):
            reqs.append(("Permissions.Modify", f"/sdn/zones/{zone}"))  # ACL de grupo sobre la vnet
        return reqs
    if action == "deploy_vms":
        return [("VM.Allocate", "/")]
    return []


def preflight(bp: dict, inputs: dict) -> dict:
    errors: list = []
    warnings: list = []

    try:
        ctx = blueprint_loader.validate_inputs(bp, inputs)
    except ValueError as exc:
        return {"errors": [str(exc)], "warnings": []}
    orchestrator.compute_derived(ctx)

    steps = bp.get("steps", [])

    # ── Permisos del token ──
    # Bloqueamos SOLO cuando Proxmox confirma que faltan: si /access/permissions devuelve un
    # mapa NO vacío y un privilegio necesario no aparece en ningún path, el paso fallaría con un
    # 403 real → mejor atajarlo antes de crear nada. Si el mapa viene vacío/ilegible no podemos
    # afirmarlo, así que NO bloqueamos (evita falsos positivos) y dejamos que la API decida.
    perms = pve_client.get_permissions()
    if isinstance(perms, dict) and perms:
        missing, seen = [], set()
        for s in steps:
            try:
                p = blueprint_loader.resolve(s.get("params", {}), ctx)
            except Exception:
                p = s.get("params", {})
            for priv, path in _perm_reqs(s.get("action"), p):
                if (priv, path) in seen:
                    continue
                seen.add((priv, path))
                if not _eff_priv(perms, priv, path):
                    missing.append(f"{priv} en {path}")
        if missing:
            token_id = os.getenv("PROXMOX_TOKEN_ID", "el token de Proxmox")
            errors.append(
                f"El token «{token_id}» no tiene estos privilegios: " + ", ".join(missing) +
                ". Concédeselos en esas rutas (rol Administrator o uno que los incluya) y marca "
                "'Propagate'. Ojo: un permiso en '/' no siempre llega a /sdn; concédelo en /sdn. "
                "El blueprint actúa con ESTE token, no con tu usuario de la interfaz."
            )

    # ── Estado actual del clúster (read-only) ──
    try:
        existing_pools = set(pve_client.get_all_pools())
    except Exception:
        existing_pools = set()
    try:
        _vnets = pve_client.proxmox.cluster.sdn.vnets.get()
        existing_vnets = {v["vnet"] for v in _vnets}
        vnet_tags = {}  # zona → {tags en uso}
        for v in _vnets:
            t = str(v.get("tag", "")).strip()
            if t and t != "None":
                vnet_tags.setdefault(v.get("zone"), set()).add(int(t))
    except Exception:
        existing_vnets, vnet_tags = set(), {}
    try:
        zone_types = {z["zone"]: (z.get("type") or "") for z in pve_client.proxmox.cluster.sdn.zones.get()}
    except Exception:
        zone_types = {}
    try:
        existing_groups = {g["groupid"] for g in pve_client.proxmox.access.groups.get()}
    except Exception:
        existing_groups = set()
    try:
        existing_vms = {r.get("name") for r in pve_client.proxmox.cluster.resources.get(type="vm")
                        if r.get("type") == "qemu"}
    except Exception:
        existing_vms = set()
    try:
        nodes = set(pve_client.get_nodes())
    except Exception:
        nodes = set()

    planned_pools, planned_vnets, planned_groups, planned_vms = set(), set(), set(), set()
    _tpl_cache, _net_cache = {}, {}

    def _group_ok(label, g):
        if g and g not in existing_groups and g not in planned_groups:
            errors.append(f"{label}: el grupo '{g}' no existe ni se crea en un paso previo.")

    for s in steps:
        action, sid = s.get("action"), s.get("id")
        label = f"Paso '{sid}' ({action})"
        try:
            p = blueprint_loader.resolve(s.get("params", {}), ctx)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue

        if action == "create_group":
            gid = _opt(p.get("groupid")) or ""
            e = validation.validate_name("group", gid)
            if e:
                errors.append(f"{label}: {e}")
            else:
                if gid in existing_groups or gid in planned_groups:
                    warnings.append(f"{label}: el grupo '{gid}' ya existe (se reutiliza).")
                planned_groups.add(gid)

        elif action == "create_pool":
            poolid = _opt(p.get("poolid")) or ""
            e = validation.validate_name("pool", poolid)
            if e:
                errors.append(f"{label}: {e}")
            else:
                if poolid in existing_pools or poolid in planned_pools:
                    warnings.append(f"{label}: el pool '{poolid}' ya existe (se reutiliza).")
                planned_pools.add(poolid)
            _group_ok(label, _opt(p.get("group")))
            # Cuotas dentro de rango (si se indican explícitamente).
            for key, lbl, lo, hi in (("quota_cpu", "Cuota vCPU", 1, 4096),
                                     ("quota_ram_mb", "Cuota RAM (MB)", 256, 16777216),
                                     ("quota_disk_gb", "Cuota disco (GB)", 1, 1048576)):
                val = p.get(key)
                if val is not None and str(val).strip() not in ("", "None") and not _is_tpl(val):
                    iv = _intval(val, None)
                    if iv is None or iv < lo or iv > hi:
                        errors.append(f"{label}: {lbl} fuera de rango ({lo}-{hi}).")

        elif action == "create_vnet":
            vnet = _opt(p.get("vnet")) or ""
            # ¿es nueva? (se decide ANTES de añadirla a planned_vnets)
            is_new_vnet = vnet not in existing_vnets and vnet not in planned_vnets
            e = validation.validate_name("vnet", vnet)
            if e:
                errors.append(f"{label}: {e}")
            else:
                if not is_new_vnet:
                    warnings.append(f"{label}: la VNet '{vnet}' ya existe (se reutiliza).")
                planned_vnets.add(vnet)
            zone = _opt(p.get("zone")) or ""
            if not zone:
                errors.append(f"{label}: falta la zona SDN (elígela del desplegable).")
            else:
                ze = validation.validate_name("zone", zone)
                if ze:
                    errors.append(f"{label}: {ze}")
                elif zone_types and zone not in zone_types:
                    errors.append(f"{label}: la zona SDN '{zone}' no existe. Créala en Proxmox "
                                  "(Datacenter → SDN → Zones); los blueprints no crean zonas.")
            # Tag/VNI: en zonas vlan/vxlan/qinq/evpn es obligatorio (se autoasigna si se omite).
            # Si se indica explícito, debe ser numérico y no colisionar. Solo se aplica a vnets
            # NUEVAS: si la vnet ya existe se reutiliza (no se reasigna tag) → no hay colisión.
            tag = _opt(p.get("tag"))
            if tag is not None:
                if not tag.isdigit():
                    errors.append(f"{label}: el tag debe ser un número (VLAN id / VNI).")
                elif (is_new_vnet and zone_types.get(zone) in ("vlan", "vxlan", "qinq", "evpn")
                      and int(tag) in vnet_tags.get(zone, set())):
                    errors.append(f"{label}: el tag {tag} ya está en uso en la zona '{zone}'. "
                                  "Déjalo vacío para autoasignar uno libre.")
            subnet = _opt(p.get("subnet"))
            if subnet:
                ce = validation.validate_cidr(subnet)
                if ce:
                    errors.append(f"{label}: {ce}")
            _group_ok(label, _opt(p.get("group")))

        elif action == "deploy_vms":
            base = _opt(p.get("base_name")) or ""
            e = validation.validate_name("vm", base)
            if e:
                errors.append(f"{label}: {e}")
            count = _intval(p.get("count"), 1)
            if count < 1 or count > orchestrator.MAX_VMS_PER_STEP:
                errors.append(f"{label}: nº de instancias fuera de rango (1-{orchestrator.MAX_VMS_PER_STEP}).")
            node = _opt(p.get("node"))
            if not node:
                errors.append(f"{label}: falta el nodo.")
            elif node not in nodes:
                errors.append(f"{label}: el nodo '{node}' no existe.")
            else:
                tpl = _opt(p.get("template_id"))
                if not tpl:
                    errors.append(f"{label}: falta la plantilla.")
                else:
                    if node not in _tpl_cache:
                        try:
                            _tpl_cache[node] = {str(v) for v in pve_client.get_templates(node).values()}
                        except Exception:
                            _tpl_cache[node] = set()
                    if tpl not in _tpl_cache[node]:
                        errors.append(f"{label}: la plantilla '{tpl}' no existe en el nodo '{node}'.")
                bridges = p.get("bridges") or []
                if isinstance(bridges, str):
                    bridges = [bridges]
                if not bridges:
                    errors.append(f"{label}: falta al menos una red.")
                else:
                    if node not in _net_cache:
                        try:
                            n = pve_client.get_networks(node)
                            _net_cache[node] = set(n.get("bridges", [])) | set(n.get("vnets", []))
                        except Exception:
                            _net_cache[node] = set()
                    for b in bridges:
                        b = str(b).strip()
                        if b and b not in _net_cache[node] and b not in planned_vnets:
                            errors.append(f"{label}: la red '{b}' no existe ni se crea en un paso previo.")
            pool = _opt(p.get("pool"))
            if pool and pool not in existing_pools and pool not in planned_pools:
                errors.append(f"{label}: el pool '{pool}' no existe ni se crea en un paso previo.")
            if base:
                for i in range(count):
                    name = f"{base}-{i + 1}"
                    if name in existing_vms:
                        errors.append(f"{label}: ya existe una VM llamada '{name}' (¿blueprint ya lanzado?).")
                    planned_vms.add(name)
            # IP estática: inicio en CIDR (con máscara) y puerta de enlace válida.
            if (_opt(p.get("ip_mode")) or "dhcp").lower() == "static":
                ip_start = _opt(p.get("ip_start"))
                if not ip_start:
                    errors.append(f"{label}: con IP estática debes indicar la IP inicial en CIDR (ej. 10.0.0.10/24).")
                elif not _is_tpl(ip_start):
                    ce = validation.validate_static_cidr(ip_start)
                    if ce:
                        errors.append(f"{label}: {ce}")
                gw = _opt(p.get("gateway"))
                if gw and not _is_tpl(gw):
                    ge = validation.validate_ip(gw)
                    if ge:
                        errors.append(f"{label}: puerta de enlace — {ge}")
            # DNS (en cualquier modo): cada servidor debe ser una IP válida.
            for d in _aslist(p.get("dns")):
                if not _is_tpl(d) and validation.validate_ip(d):
                    errors.append(f"{label}: DNS — {validation.validate_ip(d)}")
            # Recursos dentro de rango.
            for key, lbl, lo, hi in (("cpu", "vCPU", 1, 512),
                                     ("ram_mb", "RAM (MB)", 128, 4194304),
                                     ("disk_gb", "Disco (GB)", 1, 1048576)):
                val = p.get(key)
                if val is not None and str(val).strip() not in ("", "None") and not _is_tpl(val):
                    iv = _intval(val, None)
                    if iv is None or iv < lo or iv > hi:
                        errors.append(f"{label}: {lbl} fuera de rango ({lo}-{hi}).")
            # Usuario admin no vacío (si se especifica explícitamente).
            if p.get("admin_user") is not None and str(p.get("admin_user")).strip() == "":
                errors.append(f"{label}: el usuario admin no puede estar vacío.")
            # TTL ≥ 0 (0 = persistente).
            ttl = p.get("ttl_hours")
            if ttl is not None and str(ttl).strip() not in ("", "None") and not _is_tpl(ttl):
                tv = _intval(ttl, None)
                if tv is None or tv < 0:
                    errors.append(f"{label}: TTL inválido (horas ≥ 0; 0 = persistente).")

        elif action in ("install_apps", "power_action", "create_snapshot"):
            target = _opt(p.get("target_vm"))
            if not target:
                errors.append(f"{label}: falta la VM destino.")
            elif target not in existing_vms and target not in planned_vms:
                errors.append(f"{label}: la VM destino '{target}' no existe ni se crea en un paso previo.")

    return {"errors": errors, "warnings": warnings}
