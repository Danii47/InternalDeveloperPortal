import os
import json
import logging
import subprocess
from dataclasses import dataclass, field, replace
from typing import List, Tuple
from dotenv import load_dotenv

from core.config import PROXMOX_CA_PATH, PROXMOX_VERIFY_SSL

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class VMConfig:
    """
    Esquema único de las variables de una VM. Es la fuente de verdad tanto para el
    despliegue (datos del formulario) como para las operaciones que reconstruyen el
    estado (update/reprovision/audit, vía `_config_from_state`).

    Para añadir una variable cloud-init nueva basta con:
      1) añadir el campo aquí,
      2) declararla en terraform/vm_base/variables.tf y usarla en main.tf,
      3) mapearla en `_render_vars` (una línea),
      4) preservarla en `_config_from_state` (para que no haya drift),
      5) (si viene del formulario) pasarla en routers/deploy.py.
    """
    vm_id: int
    node_name: str
    template_id: int
    vm_name: str
    network_bridge: str
    admin_user: str
    admin_password: str
    network_bridges: List[str] = field(default_factory=list)  # multi-NIC; vacío = [network_bridge]
    vm_ip: str = "dhcp"
    vm_gateway: str = ""
    vm_dns: List[str] = field(default_factory=list)
    vm_dns_domain: str = ""   # dominio de búsqueda DNS (vacío = ninguno)
    vm_ram: int = 2048
    vm_cpu: int = 2
    disk_size: int = 20
    vm_pool: str = ""         # ID del Resource Pool del propietario (vacío = sin pool)
    app_snippet_id: str = ""  # Volid del snippet vendor-data de la 1-Click App (vacío = sin app)
    ssh_keys: str = ""        # Clave(s) SSH pública(s), una por línea (vacío = ninguna)


class TerraformRunner:
    """
    Clase encargada de ejecutar los comandos de Terraform de forma segura.
    """
    def __init__(self, tf_directory: str):
        self.tf_directory = tf_directory
        if not os.path.isdir(self.tf_directory):
            raise FileNotFoundError(f" El directorio Terraform no existe: {self.tf_directory}")

        load_dotenv()
        self.api_token = os.getenv("PROXMOX_TOKEN_ID") + "=" + os.getenv("PROXMOX_SECRET_TOKEN")
        if not self.api_token or "=" not in self.api_token:
            raise ValueError(" Faltan credenciales de Proxmox en el entorno (.env).")
        self.endpoint = os.getenv("PROXMOX_URL", "")

    def _base_env(self) -> dict:
        """Entorno base para toda invocación de Terraform: credenciales + endpoint del provider.
        Centraliza los TF_VAR_* comunes (evita hardcodear el endpoint en providers.tf)."""
        env = os.environ.copy()
        env.update({
            "TF_VAR_api_token": self.api_token,
            "TF_VAR_proxmox_endpoint": self.endpoint,
            # Verificación TLS configurable (default inseguro = comportamiento histórico).
            "TF_VAR_proxmox_insecure": "false" if (PROXMOX_VERIFY_SSL or PROXMOX_CA_PATH) else "true",
        })
        return env

    # ── Mapeo VMConfig ↔ Terraform (único lugar) ────────────────────────────────

    def _render_vars(self, c: VMConfig) -> Tuple[List[str], dict]:
        """
        Convierte un VMConfig en (args `-var` para argv, dict de TF_VAR_* para el entorno).
        Los valores sensibles o multilínea (contraseña, claves SSH, lista DNS) viajan por
        entorno para no quedar expuestos en argv (visibles vía `ps`).
        """
        env = {
            "TF_VAR_admin_password": c.admin_password,
            "TF_VAR_ssh_keys": c.ssh_keys,
            "TF_VAR_vm_dns": json.dumps(c.vm_dns),
            "TF_VAR_network_bridges": json.dumps(c.network_bridges),
        }
        args = [
            f"-var=vm_id={c.vm_id}",
            f"-var=node_name={c.node_name}",
            f"-var=template_id={c.template_id}",
            f"-var=vm_name={c.vm_name}",
            f"-var=vm_ip={c.vm_ip}",
            f"-var=vm_gateway={c.vm_gateway or ''}",
            f"-var=network_bridge={c.network_bridge}",
            f"-var=admin_user={c.admin_user}",
            f"-var=vm_cpu={c.vm_cpu}",
            f"-var=vm_ram={c.vm_ram}",
            f"-var=disk_size={c.disk_size}",
            f"-var=vm_pool={c.vm_pool}",
            f"-var=app_snippet_id={c.app_snippet_id}",
            f"-var=vm_dns_domain={c.vm_dns_domain}",
        ]
        return args, env

    def _load_vm_attrs(self, vm_name: str) -> dict:
        """
        Lee el tfstate del workspace de la VM y devuelve los `attributes` del recurso.
        Lanza RuntimeError si no hay estado (la VM no está gestionada por el IDP).
        """
        state_path = os.path.join(self.tf_directory, "terraform.tfstate.d", vm_name, "terraform.tfstate")
        if not os.path.exists(state_path):
            raise RuntimeError(f"No se encontró el estado de Terraform para {vm_name}. ¿Es una máquina Managed?")
        with open(state_path, "r") as f:
            state = json.load(f)
        instances = [r for r in state.get("resources", []) if r.get("type") == "proxmox_virtual_environment_vm"]
        return instances[0]["instances"][0]["attributes"]

    def _config_from_state(self, vm_name: str) -> VMConfig:
        """
        Reconstruye un VMConfig desde el tfstate. Preserva TODOS los campos cloud-init
        para que update/reprovision/audit los reinyecten y no provoquen falso drift.
        """
        attrs = self._load_vm_attrs(vm_name)
        init_block = attrs.get("initialization", [{}])[0] if attrs.get("initialization") else {}
        ip_config = init_block.get("ip_config", [{}])[0].get("ipv4", [{}])[0] if init_block.get("ip_config") else {}
        dns_config = init_block.get("dns", [{}])[0] if init_block.get("dns") else {}
        user_account = init_block.get("user_account", [{}])[0] if init_block.get("user_account") else {}
        return VMConfig(
            vm_id=attrs.get("vm_id"),
            node_name=attrs.get("node_name"),
            template_id=attrs.get("clone", [{}])[0].get("vm_id", 0) if attrs.get("clone") else 0,
            vm_name=vm_name,
            network_bridge=attrs.get("network_device", [{}])[0].get("bridge", "vmbr0") if attrs.get("network_device") else "vmbr0",
            network_bridges=[d.get("bridge") for d in attrs.get("network_device", []) if d.get("bridge")],
            admin_user=user_account.get("username", "administrator"),
            admin_password=user_account.get("password", ""),
            vm_ip=ip_config.get("address", "dhcp"),
            vm_gateway=ip_config.get("gateway", "") or "",
            vm_dns=dns_config.get("servers") or [],
            vm_dns_domain=dns_config.get("domain", "") or "",
            vm_ram=attrs.get("memory", [{}])[0].get("dedicated", 1024) if attrs.get("memory") else 1024,
            vm_cpu=attrs.get("cpu", [{}])[0].get("cores", 1) if attrs.get("cpu") else 1,
            disk_size=attrs.get("disk", [{}])[0].get("size", 20) if attrs.get("disk") else 20,
            vm_pool=attrs.get("pool_id") or "",
            app_snippet_id=init_block.get("vendor_data_file_id") or "",
            ssh_keys="\n".join(user_account.get("keys") or []),
        )

    def _select_workspace(self, env: dict, vm_name: str, create: bool = False) -> None:
        """Selecciona (o crea) el workspace por-VM. Centraliza el patrón repetido."""
        if create:
            try:
                subprocess.run(
                    ["terraform", "workspace", "select", "-or-create", vm_name],
                    cwd=self.tf_directory, env=env, capture_output=True, check=True,
                )
                return
            except subprocess.CalledProcessError:
                subprocess.run(["terraform", "workspace", "new", vm_name],
                               cwd=self.tf_directory, env=env, capture_output=True)
        subprocess.run(["terraform", "workspace", "select", vm_name],
                       cwd=self.tf_directory, env=env, capture_output=True, check=create is False)

    def _select_default_workspace(self, env: dict) -> None:
        subprocess.run(["terraform", "workspace", "select", "default"],
                       cwd=self.tf_directory, env=env, capture_output=True)

    # ── Operaciones ─────────────────────────────────────────────────────────────

    def deploy(self, config: VMConfig) -> bool:
        env = self._base_env()

        workspace_name = config.vm_name
        self._select_workspace(env, workspace_name, create=True)

        args, env_extra = self._render_vars(config)
        env.update(env_extra)
        cmd = ["terraform", "apply", "-auto-approve", *args]

        logger.info(f"🚀 Deploying VM '{config.vm_name}' (ID: {config.vm_id}) in workspace '{workspace_name}'...")

        try:
            subprocess.run(cmd, cwd=self.tf_directory, env=env, capture_output=True, text=True, check=True)
            logger.info(f"✅ Deployment successful: {config.vm_name}")
            self._select_default_workspace(env)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Critical failure deploying {config.vm_name}.")
            logger.error(f"Terraform Error Output:\n{e.stderr}")
            self._select_default_workspace(env)
            raise RuntimeError(f"Terraform failed: {e.stderr}") from e

    def destroy(self, vm_name: str, vmid: int, node_name: str) -> bool:
        env = self._base_env()

        logger.info(f"💣 Iniciando destrucción de la máquina: {vm_name} (ID: {vmid}) en {node_name}")

        # Teardown: solo importan el nombre/id/nodo; el resto son valores ficticios
        # para satisfacer las variables requeridas por variables.tf.
        destroy_args = [
            "terraform", "destroy", "-auto-approve", "-input=false",
            f"-var=vm_name={vm_name}",
            f"-var=vm_id={vmid}",
            f"-var=node_name={node_name}",
            "-var=template_id=0",
            "-var=vm_dns=[]",
            "-var=network_bridge=dummy",
            "-var=admin_password=dummy_password_no_usada",
            "-var=admin_user=dummy_user",
            "-var=vm_cpu=1",
            "-var=vm_ram=1024",
            "-var=vm_pool=",
            "-var=app_snippet_id=",
        ]

        try:
            self._select_workspace(env, vm_name)
            logger.info("Ejecutando terraform destroy...")
            subprocess.run(destroy_args, cwd=self.tf_directory, env=env, capture_output=True, text=True, check=True)

            self._select_default_workspace(env)
            subprocess.run(["terraform", "workspace", "delete", vm_name], cwd=self.tf_directory, env=env, capture_output=True)

            logger.info(f"✅ Máquina {vm_name} destruida y workspace eliminado con éxito.")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Error crítico destruyendo {vm_name}:\n{e.stderr}")
            self._select_default_workspace(env)
            raise RuntimeError(f"Fallo al destruir la infraestructura: {e.stderr}") from e

    def update(self, vm_name: str, updates: dict) -> bool:
        env = self._base_env()

        logger.info(f"⚙️ Iniciando mutación de la máquina: {vm_name}")

        base = self._config_from_state(vm_name)
        # Usa las claves del formulario de edición si las hay; si no, preserva las del estado.
        req_ssh = updates.get("ssh_keys", "").strip()
        # admin_* en dummy: el resize no toca la identidad ya provisionada en el primer arranque.
        cfg = replace(
            base,
            admin_user="dummy",
            admin_password="dummy",
            vm_cpu=updates.get("vm_cpu"),
            vm_ram=updates.get("vm_ram_mb"),
            disk_size=updates.get("disk_size_gb"),
            ssh_keys=req_ssh or base.ssh_keys,
        )

        try:
            args, env_extra = self._render_vars(cfg)
            env.update(env_extra)
            apply_args = ["terraform", "apply", "-auto-approve", "-input=false", *args]

            self._select_workspace(env, vm_name)
            logger.info("Aplicando cambios en la infraestructura...")
            subprocess.run(apply_args, cwd=self.tf_directory, env=env, capture_output=True, text=True, check=True)
            self._select_default_workspace(env)

            logger.info(f"✅ Mutación de {vm_name} completada con éxito.")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Error crítico mutando {vm_name}:\n{e.stderr}")
            self._select_default_workspace(env)
            raise RuntimeError(f"Fallo al aplicar cambios en Terraform:\n{e.stderr}") from e

    def reprovision(self, vm_name: str, new_password: str) -> bool:
        env = self._base_env()

        logger.info(f"🔄 Iniciando reprovisionamiento (Nuke & Pave) de: {vm_name}")

        # Recrea el recurso con la misma configuración del estado, cambiando la contraseña.
        cfg = replace(self._config_from_state(vm_name), admin_password=new_password)

        try:
            args, env_extra = self._render_vars(cfg)
            env.update(env_extra)
            apply_args = [
                "terraform", "apply", "-auto-approve", "-input=false",
                "-replace=proxmox_virtual_environment_vm.server",
                *args,
            ]

            self._select_workspace(env, vm_name)
            logger.info("Aplicando Nuke & Pave en la infraestructura...")
            subprocess.run(apply_args, cwd=self.tf_directory, env=env, capture_output=True, text=True, check=True)
            self._select_default_workspace(env)

            logger.info(f"✅ Reprovisionamiento de {vm_name} completado con éxito.")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Error crítico reprovisionando {vm_name}:\n{e.stderr}")
            self._select_default_workspace(env)
            raise RuntimeError(f"Fallo al aplicar reprovisionamiento en Terraform:\n{e.stderr}") from e

    def audit(self, vm_name: str) -> dict:
        env = self._base_env()

        logger.info(f"🛡️ Iniciando auditoría de drift para: {vm_name}")

        cfg = self._config_from_state(vm_name)

        try:
            args, env_extra = self._render_vars(cfg)
            env.update(env_extra)
            plan_args = ["terraform", "plan", "-detailed-exitcode", "-input=false", "-out=tfplan.cache", *args]

            self._select_workspace(env, vm_name)

            logger.info("Ejecutando terraform plan para buscar Drift...")
            res = subprocess.run(plan_args, cwd=self.tf_directory, env=env, capture_output=True, text=True)

            if res.returncode == 0:
                return {"drift": False, "changes": []}
            elif res.returncode == 2:
                show_res = subprocess.run(["terraform", "show", "-json", "tfplan.cache"], cwd=self.tf_directory, env=env, capture_output=True, text=True, check=True)
                plan_data = json.loads(show_res.stdout)

                changes = []
                for res_change in plan_data.get("resource_changes", []):
                    if res_change["type"] == "proxmox_virtual_environment_vm":
                        before = res_change.get("change", {}).get("before", {})
                        after = res_change.get("change", {}).get("after", {})

                        cpu_before = before.get("cpu", [{}])[0].get("cores") if before.get("cpu") else None
                        cpu_after = after.get("cpu", [{}])[0].get("cores") if after.get("cpu") else None
                        if cpu_before != cpu_after:
                            changes.append({"parameter": "cpu", "label": "vCPU Cores", "actual": cpu_before, "expected": cpu_after})

                        mem_before = before.get("memory", [{}])[0].get("dedicated") if before.get("memory") else None
                        mem_after = after.get("memory", [{}])[0].get("dedicated") if after.get("memory") else None
                        if mem_before != mem_after:
                            changes.append({"parameter": "ram", "label": "Memoria RAM (MB)", "actual": mem_before, "expected": mem_after})

                        disk_before = before.get("disk", [{}])[0].get("size") if before.get("disk") else None
                        disk_after = after.get("disk", [{}])[0].get("size") if after.get("disk") else None
                        if disk_before != disk_after:
                            changes.append({"parameter": "disk", "label": "Disco Raíz (GB)", "actual": disk_before, "expected": disk_after})

                return {"drift": True, "changes": changes}
            else:
                raise RuntimeError(f"Error ejecutando terraform plan: {res.stderr}")

        except Exception as e:
            logger.error(f"❌ Error en auditoría de {vm_name}:\n{str(e)}")
            raise e
        finally:
            self._select_default_workspace(env)
            try:
                os.remove(os.path.join(self.tf_directory, "tfplan.cache"))
            except Exception:
                pass

    def adopt(self, vm_name: str, node: str, vmid: int, template_id: int = 0) -> None:
        """
        Imports an existing Proxmox VM into Terraform management without changing it.

        Runs `terraform import proxmox_virtual_environment_vm.server <node>/<vmid>` in
        a new workspace, creating the tfstate from the VM's current live state in Proxmox.
        The tag 'managed-by-idp' is added separately by the caller (ProxmoxIDPClient).

        template_id = 0  → VM has no clone origin (dynamic clone block is skipped).
        template_id > 0  → VM was cloned from this template (enables reprovision later).
        """
        env = self._base_env()

        logger.info(f"📥 Adoptando VM '{vm_name}' (ID: {vmid}, nodo: {node}, template: {template_id})")

        self._select_workspace(env, vm_name, create=True)

        # Guard: skip import if state already exists (idempotent re-adoption)
        state_path = os.path.join(self.tf_directory, "terraform.tfstate.d", vm_name, "terraform.tfstate")
        if os.path.exists(state_path):
            logger.warning(f"⚠️  Estado de Terraform ya existe para '{vm_name}'. Saltando import.")
            self._select_default_workspace(env)
            return

        import_args = [
            "terraform", "import",
            f"-var=vm_name={vm_name}",
            f"-var=vm_id={vmid}",
            f"-var=node_name={node}",
            f"-var=template_id={template_id}",
            "-var=network_bridge=dummy",    # required by variables.tf, not used during import
            "-var=admin_password=dummy",    # required by variables.tf, sensitive, not used during import
            "-var=vm_dns=[]",               # required by variables.tf, not used during import
            "-var=vm_pool=",
            "-var=app_snippet_id=",         # required by variables.tf, not used during import
            "proxmox_virtual_environment_vm.server",
            f"{node}/{vmid}",               # bpg/proxmox import ID format
        ]

        try:
            result = subprocess.run(
                import_args,
                cwd=self.tf_directory, env=env,
                capture_output=True, text=True, check=True
            )
            logger.info(f"✅ Import completado para '{vm_name}': {result.stdout.strip()}")
        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Error en terraform import para '{vm_name}':\n{e.stderr}")
            self._select_default_workspace(env)
            raise RuntimeError(f"Terraform import falló: {e.stderr}") from e

        self._select_default_workspace(env)
