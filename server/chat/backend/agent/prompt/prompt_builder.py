from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, List, Optional, Tuple

# Prefix Cache Configuration
PREFIX_CACHE_EPHEMERAL_TTL = 300  # 5 minutes - TTL for ephemeral cache segments

from chat.backend.agent.utils.prefix_cache import PrefixCacheManager
from utils.db.connection_pool import db_pool


@dataclass
class PromptSegments:
    system_invariant: str
    provider_constraints: str
    regional_rules: str
    ephemeral_rules: str
    long_documents_note: str
    provider_context: str
    prerequisite_checks: str
    terraform_validation: str
    model_overlay: str
    failure_recovery: str
    github_context: str
    bitbucket_context: str = ""
    manual_vm_access: str = ""  # Manual VM access hints with managed keys
    kubectl_onprem: str = ""
    background_mode: str = ""  # Background chat autonomous operation instructions
    knowledge_base_memory: str = ""  # User's knowledge base memory context


def _normalize_providers(provider_preference: Optional[Any]) -> List[str]:
    if provider_preference is None:
        return []
    if isinstance(provider_preference, str):
        provider_iterable = [provider_preference]
    elif isinstance(provider_preference, list):
        provider_iterable = provider_preference
    else:
        provider_iterable = []

    normalized: List[str] = []
    for item in provider_iterable:
        if not item:
            continue
        candidate = str(item).strip().lower()
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def build_provider_constraints(provider_preference: Optional[Any]) -> Tuple[str, str, str]:
    """Return provider_text, provider_restrictions, and combined provider_constraints segment."""
    normalized = _normalize_providers(provider_preference)

    if normalized:
        if len(normalized) == 1:
            provider_text = f"the {normalized[0]} cloud"
            provider_restrictions = f"- You can ONLY access tools for the {normalized[0]} provider\n"
        else:
            provider_list = ", ".join(normalized)
            provider_text = f"multiple clouds: {provider_list}"
            provider_restrictions = f"- You can access tools for the following providers: {provider_list}\n"
    else:
        provider_text = "no specific cloud"
        provider_restrictions = "- If no provider is selected, you have limited tool access\n"

    provider_constraints = (
        f"IMPORTANT: You are currently operating on {provider_text}. "
        "All resources you create or manage MUST be for the selected provider(s). For example, if the provider is 'azure', use 'azurerm' resources. If it is 'gcp', use 'google' resources.\n\n"
        "PROVIDER RESTRICTIONS:\n"
        f"{provider_restrictions}"
        "- If no provider is selected, you have limited tool access\n"
        "- All cloud operations are restricted to the user's selected provider(s)\n"
        "- No fallbacks or cross-provider operations are allowed unless multiple providers are explicitly selected\n"
    )
    return provider_text, provider_restrictions, provider_constraints


def build_provider_context_segment(provider_preference: Optional[Any], selected_project_id: Optional[str], mode: Optional[str] = None) -> str:
    normalized = _normalize_providers(provider_preference)
    normalized_mode = (mode or "agent").strip().lower()
    
    if not normalized and not selected_project_id:
        return ""

    parts: List[str] = ["PROVIDER CONTEXT:\n"]

    if normalized:
        providers_text = ", ".join(normalized)
        parts.append(
            f"- Provider already selected: {providers_text}. Do NOT ask the user to choose a provider again; continue with these settings.\n"
        )
        # Add explicit instruction about which provider to use for cloud_exec
        if len(normalized) == 1:
            parts.append(
                f"- IMPORTANT: Use provider='{normalized[0]}' for all cloud_exec calls.\n"
            )

    if selected_project_id:
        parts.append(
            f"- Active project/subscription: {selected_project_id}. Reuse this identifier in every command or Terraform manifest instead of placeholders.\n"
        )
    else:
        for provider in normalized or ["unknown"]:
            if provider == "gcp":
                parts.append(
                    "- IMPORTANT: If the user explicitly specifies a GCP project, set it as active: cloud_exec('gcp', 'config set project PROJECT_ID').\n"
                    "- Only if NO project is specified by the user, fetch the current project: cloud_exec('gcp', 'config get-value project'). Use the returned value immediately.\n"
                )
            elif provider == "aws":
                parts.append(
                    "- **MULTI-ACCOUNT AWS**: You have multiple AWS accounts connected.\n"
                    "  1. Your FIRST cloud_exec('aws', ...) call (without account_id) automatically queries ALL accounts in parallel and returns `results_by_account`.\n"
                    "  2. Review the per-account results to identify which account(s) are relevant.\n"
                    "  3. For ALL subsequent calls, pass `account_id='<ACCOUNT_ID>'` to target only the relevant account(s). Example: cloud_exec('aws', 'ec2 describe-instances', account_id='123456789012')\n"
                    "  4. NEVER keep querying all accounts after you've identified the relevant one -- it wastes time and adds noise.\n"
                    "- Fetch the AWS account ID before writing Terraform: cloud_exec('aws', \"sts get-caller-identity --query 'Account' --output text\"). Store and reuse that output.\n"
                )
            elif provider == "azure":
                parts.append(
                    "- Fetch the Azure subscription before writing Terraform: cloud_exec('azure', \"account show --query 'id' -o tsv\"). Use the concrete subscription ID in code.\n"
                )
            elif provider == "ovh":
                parts.append(
                    "## OVHcloud Reference:\n\n"
                    "### CLI COMMANDS (use cloud_exec with 'ovh'):\n\n"
                    "**Discovery Commands:**\n"
                    "- List projects: `cloud_exec('ovh', 'cloud project list --json')`\n"
                    "- List regions: `cloud_exec('ovh', 'cloud region list --cloud-project <PROJECT_ID> --json')`\n"
                    "- List flavors: `cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <PROJECT_ID> --region <REGION> --json')`\n"
                    "- List images: `cloud_exec('ovh', 'cloud reference list-images --cloud-project <PROJECT_ID> --region <REGION> --json')`\n\n"
                    "**Instance Management:**\n"
                    "- List instances: `cloud_exec('ovh', 'cloud instance list --cloud-project <PROJECT_ID> --json')`\n"
                    "- Create instance: `cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --network.public --wait --json')`\n"
                    "- With SSH key: `cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --ssh-key.create.name my-key --ssh-key.create.public-key \"<PUBKEY>\" --network.public --wait --json')`\n"
                    "- Stop/Start/Reboot: `cloud_exec('ovh', 'cloud instance stop|start|reboot <INSTANCE_ID> --cloud-project <PROJECT_ID>')`\n"
                    "- Delete: `cloud_exec('ovh', 'cloud instance delete <INSTANCE_ID> --cloud-project <PROJECT_ID>')`\n\n"
                    "**Kubernetes (MKS):**\n"
                    "- List clusters: `cloud_exec('ovh', 'cloud kube list --cloud-project <PROJECT_ID> --json')`\n"
                    "- Create cluster: `cloud_exec('ovh', 'cloud kube create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --version 1.28')`\n"
                    "- Get kubeconfig: `cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>')`\n"
                    "- Create nodepool: `cloud_exec('ovh', 'cloud kube nodepool create <CLUSTER_ID> --cloud-project <PROJECT_ID> --name worker-pool --flavor b2-7 --desired-nodes 3 --autoscale true')`\n\n"
                    "**KUBECTL WORKFLOW (for OVH clusters):**\n"
                    "1. Save kubeconfig to file: `cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>', output_file='/tmp/kubeconfig.yaml')`\n"
                    "2. Run kubectl: `terminal_exec('kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -A')`\n"
                    "3. CRITICAL: Use output_file parameter to save kubeconfig directly - avoids shell escaping issues\n"
                    "4. Do NOT try to embed kubeconfig YAML in echo commands - it will break due to special characters\n\n"
                    "**Networks:**\n"
                    "- List networks: `cloud_exec('ovh', 'cloud network list --cloud-project <PROJECT_ID> --json')`\n"
                    "- Create network: `cloud_exec('ovh', 'cloud network create --cloud-project <PROJECT_ID> --name <NAME> --vlan-id <ID> --regions <REGION>')`\n\n"
                    "**Object Storage (S3):**\n"
                    "- List S3 users: `cloud_exec('ovh', 'cloud storage-s3 list --cloud-project <PROJECT_ID> --json')`\n"
                    "- Create S3 user: `cloud_exec('ovh', 'cloud storage-s3 create --cloud-project <PROJECT_ID> --region <REGION>')`\n\n"
                    "### TERRAFORM FOR OVH:\n"
                    "Use iac_tool - provider.tf is AUTO-GENERATED, just write the resource!\n"
                    "**INSTANCE EXAMPLE (MUST use nested blocks, NOT flat attributes):**\n"
                    "```hcl\n"
                    "resource \"ovh_cloud_project_instance\" \"vm\" {{\n"
                    "  service_name   = \"<PROJECT_ID>\"\n"
                    "  region         = \"US-EAST-VA-1\"\n"
                    "  billing_period = \"hourly\"\n"
                    "  name           = \"my-vm\"\n"
                    "  flavor {{\n"
                    "    flavor_id = \"<FLAVOR_UUID>\"\n"
                    "  }}\n"
                    "  boot_from {{\n"
                    "    image_id = \"<IMAGE_UUID>\"\n"
                    "  }}\n"
                    "  network {{\n"
                    "    public = true\n"
                    "  }}\n"
                    "  # SSH key options (use ONE):\n"
                    "  # Option 1: Reference existing SSH key by name\n"
                    "  ssh_key {{\n"
                    "    name = \"my-ssh-key\"  # Must exist in OVH first\n"
                    "  }}\n"
                    "  # Option 2: Create new SSH key inline\n"
                    "  # ssh_key_create {{\n"
                    "  #   name = \"my-new-key\"\n"
                    "  #   public_key = \"ssh-rsa AAAA...\"\n"
                    "  # }}\n"
                    "}}\n"
                    "```\n"
                    "**SSH KEY IMPORTANT:** Use `ssh_key` to reference existing key, or `ssh_key_create` to create new one inline. If unsure, query Context7 with topic='ovh_cloud_project_instance ssh_key'.\n"
                    "**Other resources:** `ovh_cloud_project_kube`, `ovh_cloud_project_kube_nodepool`, `ovh_cloud_project_database`\n"
                    "DO NOT write terraform{{}} or provider{{}} blocks - they are auto-generated!\n\n"
                    "### CRITICAL RULES:\n"
                    "- Use **UUID** from 'id' field for flavor/image, NOT names!\n"
                    "- Use `--cloud-project <ID>` NOT `--project-id`\n"
                    "- Region is POSITIONAL in create commands: `cloud instance create <REGION> ...`\n"
                    "- Use `kube` NOT `kubernetes` subcommand\n"
                    "- Use `--network.public` for public IP (not `--network <ID>`)\n\n"
                    "### DYNAMIC/RUNTIME DATA (versions, flavors, images):\n"
                    "Context7 docs do NOT contain runtime data. For dynamic values, use CLI:\n"
                    "- **K8s versions**: For Terraform, omit `version` to use latest stable, or use `1.31`, `1.32` (check `cloud kube create --help` for valid versions)\n"
                    "- **Flavors**: `cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <ID> --region <REGION> --json')`\n"
                    "- **Images**: `cloud_exec('ovh', 'cloud reference list-images --cloud-project <ID> --region <REGION> --json')`\n"
                    "- **Regions**: `cloud_exec('ovh', 'cloud region list --cloud-project <ID> --json')`\n"
                    "Always query flavors/images/regions BEFORE creating resources.\n\n"
                    "###️ MANDATORY: ON ANY OVH ERROR OR FAILURE:\n"
                    "**YOU MUST** use Context7 MCP to look up correct syntax BEFORE retrying. Choose the RIGHT library:\n\n"
                    "**If `iac_tool` (Terraform) fails** → Use TERRAFORM docs:\n"
                    "`mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/terraform-provider-ovh', topic='ovh_cloud_project_instance')`\n"
                    "Topic should be the **resource type** (e.g., 'ovh_cloud_project_instance', 'ovh_cloud_project_kube', 'ssh_key block')\n\n"
                    "**If `cloud_exec` (CLI) fails** → Use CLI docs:\n"
                    "`mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/ovhcloud-cli', topic='cloud instance create')`\n"
                    "Topic should be the **CLI command** (e.g., 'cloud instance create', 'cloud kube list')\n\n"
                    "️ Do NOT mix them up! Terraform errors need Terraform docs, CLI errors need CLI docs.\n"
                )
            elif provider == "scaleway":
                parts.append(
                    "## Scaleway Reference:\n\n"
                    "### CLI COMMANDS (use cloud_exec with 'scaleway'):\n\n"
                    "**CRITICAL: Always use cloud_exec('scaleway', 'command') for Scaleway commands, NOT terminal_exec!**\n"
                    "The cloud_exec tool has your Scaleway credentials configured.\n\n"
                    "**Discovery Commands:**\n"
                    "- List projects: `cloud_exec('scaleway', 'account project list')`\n"
                    "- List zones: `cloud_exec('scaleway', 'instance zone list')`\n"
                    "- List instance types: `cloud_exec('scaleway', 'instance server-type list')`\n"
                    "- List images: `cloud_exec('scaleway', 'instance image list')`\n\n"
                    "**Instance Management:**\n"
                    "- List instances: `cloud_exec('scaleway', 'instance server list')`\n"
                    "- Create instance: `cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm')`\n"
                    "- With zone: `cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm zone=fr-par-1')`\n"
                    "- Start/Stop/Reboot: `cloud_exec('scaleway', 'instance server start|stop|reboot <SERVER_ID>')`\n"
                    "- Delete: `cloud_exec('scaleway', 'instance server delete <SERVER_ID>')`\n"
                    "- SSH into server: `cloud_exec('scaleway', 'instance server ssh <SERVER_ID>')`\n\n"
                    "**Kubernetes (Kapsule):**\n"
                    "- List clusters: `cloud_exec('scaleway', 'k8s cluster list')`\n"
                    "- Create cluster: `cloud_exec('scaleway', 'k8s cluster create name=my-cluster version=1.28 cni=cilium')`\n"
                    "- Get kubeconfig: `cloud_exec('scaleway', 'k8s kubeconfig get <CLUSTER_ID>')`\n"
                    "- List pools: `cloud_exec('scaleway', 'k8s pool list cluster-id=<CLUSTER_ID>')`\n"
                    "- Create pool: `cloud_exec('scaleway', 'k8s pool create cluster-id=<CLUSTER_ID> name=worker-pool node-type=DEV1-M size=3')`\n\n"
                    "**Object Storage:**\n"
                    "- List buckets: `cloud_exec('scaleway', 'object bucket list')`\n"
                    "- Create bucket: `cloud_exec('scaleway', 'object bucket create name=my-bucket')`\n\n"
                    "**Databases:**\n"
                    "- List instances: `cloud_exec('scaleway', 'rdb instance list')`\n"
                    "- Create instance: `cloud_exec('scaleway', 'rdb instance create name=my-db engine=PostgreSQL-15 node-type=DB-DEV-S')`\n\n"
                    "### TERRAFORM FOR SCALEWAY:\n"
                    "Use iac_tool - provider.tf is AUTO-GENERATED, just write the resource!\n"
                    "Scaleway Terraform provider: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs\n\n"
                    "**INSTANCE EXAMPLE:**\n"
                    "```hcl\n"
                    "resource \"scaleway_instance_server\" \"vm\" {{\n"
                    "  name  = \"my-vm\"\n"
                    "  type  = \"DEV1-S\"\n"
                    "  image = \"ubuntu_jammy\"\n"
                    "  # Optional: specify zone (defaults to fr-par-1)\n"
                    "  # zone = \"fr-par-1\"\n"
                    "}}\n"
                    "```\n\n"
                    "**KUBERNETES (KAPSULE) CLUSTER:**\n"
                    "```hcl\n"
                    "resource \"scaleway_k8s_cluster\" \"cluster\" {{\n"
                    "  name    = \"my-cluster\"\n"
                    "  version = \"1.28\"\n"
                    "  cni     = \"cilium\"\n"
                    "}}\n\n"
                    "resource \"scaleway_k8s_pool\" \"pool\" {{\n"
                    "  cluster_id = scaleway_k8s_cluster.cluster.id\n"
                    "  name       = \"worker-pool\"\n"
                    "  node_type  = \"DEV1-M\"\n"
                    "  size       = 3\n"
                    "}}\n"
                    "```\n\n"
                    "**OBJECT STORAGE BUCKET:**\n"
                    "```hcl\n"
                    "resource \"scaleway_object_bucket\" \"bucket\" {{\n"
                    "  name = \"my-bucket\"\n"
                    "}}\n"
                    "```\n\n"
                    "**DATABASE (RDB) INSTANCE:**\n"
                    "```hcl\n"
                    "resource \"scaleway_rdb_instance\" \"db\" {{\n"
                    "  name           = \"my-database\"\n"
                    "  engine         = \"PostgreSQL-15\"\n"
                    "  node_type      = \"DB-DEV-S\"\n"
                    "  is_ha_cluster  = false\n"
                    "  disable_backup = false\n"
                    "}}\n"
                    "```\n\n"
                    "**Common Scaleway Terraform resources:**\n"
                    "- `scaleway_instance_server` - Virtual machines\n"
                    "- `scaleway_instance_ip` - Public IP addresses\n"
                    "- `scaleway_instance_security_group` - Firewall rules\n"
                    "- `scaleway_k8s_cluster` - Kubernetes clusters\n"
                    "- `scaleway_k8s_pool` - Kubernetes node pools\n"
                    "- `scaleway_object_bucket` - Object storage buckets\n"
                    "- `scaleway_rdb_instance` - Managed databases\n"
                    "- `scaleway_vpc_private_network` - Private networks\n"
                    "- `scaleway_lb` - Load balancers\n\n"
                    "DO NOT write terraform{{}} or provider{{}} blocks - they are auto-generated!\n"
                    "When to use Terraform vs CLI:\n"
                    "- **CLI (cloud_exec)**: Quick single resource ops, listing, inspection\n"
                    "- **Terraform (iac_tool)**: Complex deployments, multi-resource setups, user explicitly requests 'terraform' or 'IaC'\n\n"
                    "### CRITICAL RULES:\n"
                    "- **ALWAYS** use `cloud_exec('scaleway', ...)` NOT `terminal_exec` for Scaleway commands!\n"
                    "- Scaleway CLI uses `key=value` syntax, NOT `--key value` for most parameters\n"
                    "- Common instance types: DEV1-S, DEV1-M, DEV1-L, GP1-XS, GP1-S, GP1-M\n"
                    "- Common images: ubuntu_jammy, ubuntu_focal, debian_bookworm, debian_bullseye\n"
                    "- Default region: fr-par, zones: fr-par-1, fr-par-2, fr-par-3\n"
                    "- Default SSH username for instances: `root`\n\n"
                )
            elif provider == "tailscale":
                parts.append(
                    "## Tailscale Reference:\n\n"
                    "Tailscale is a mesh VPN/network provider. It connects your devices into a secure private network called a 'tailnet'.\n"
                    "Unlike cloud providers (GCP, AWS, Azure), Tailscale doesn't provision infrastructure - it networks existing devices.\n\n"
                    "### DEVICE MANAGEMENT:\n"
                    "- List all devices: `cloud_exec('tailscale', 'device list')`\n"
                    "- Get device details: `cloud_exec('tailscale', 'device get <DEVICE_ID>')`\n"
                    "- Authorize a device: `cloud_exec('tailscale', 'device authorize <DEVICE_ID>')`\n"
                    "- Delete a device: `cloud_exec('tailscale', 'device delete <DEVICE_ID>')`\n"
                    "- Set device tags: `cloud_exec('tailscale', 'device tag <DEVICE_ID> tag:server')`\n\n"
                    "### SSH ACCESS (execute commands on devices):\n"
                    "- Run command on device: `tailscale_ssh('hostname', 'command', 'user')`\n"
                    "- Example - check uptime: `tailscale_ssh('myserver', 'uptime', 'root')`\n"
                    "- Example - docker status: `tailscale_ssh('web-prod', 'docker ps', 'admin')`\n"
                    "- Example - disk usage: `tailscale_ssh('database-1', 'df -h', 'ubuntu')`\n"
                    "- SETUP REQUIRED: User must add Aurora's SSH public key to target devices\n"
                    "  (Get key from Settings > Tailscale > SSH Setup)\n"
                    "- Targets must have SSH server running (Linux: sshd, macOS: Remote Login)\n"
                    "- If 'Permission denied' error: remind user to add Aurora's SSH key to the device\n\n"
                    "### AUTH KEYS (for adding devices programmatically):\n"
                    "- List auth keys: `cloud_exec('tailscale', 'key list')`\n"
                    "- Create auth key: `cloud_exec('tailscale', 'key create --ephemeral --reusable --tags tag:server')`\n"
                    "- Delete auth key: `cloud_exec('tailscale', 'key delete <KEY_ID>')`\n\n"
                    "### ACL (Access Control Lists):\n"
                    "- Get current ACL: `cloud_exec('tailscale', 'acl get')`\n"
                    "- Update ACL: `cloud_exec('tailscale', 'acl set <ACL_JSON>')`\n\n"
                    "### DNS & NETWORK:\n"
                    "- Get DNS settings: `cloud_exec('tailscale', 'dns get')`\n"
                    "- List subnet routes: `cloud_exec('tailscale', 'routes list')`\n\n"
                    "### KEY CONCEPTS:\n"
                    "- **Tailnet**: Your private Tailscale network\n"
                    "- **Device**: Any machine connected to your tailnet\n"
                    "- **Tags**: Labels for devices (must start with 'tag:' prefix)\n"
                    "- **Auth Key**: Token to add devices programmatically\n"
                    "- **ACL**: Access Control List for device communication\n\n"
                    "### CRITICAL RULES:\n"
                    "- Use cloud_exec('tailscale', ...) for device/key/ACL management\n"
                    "- Use tailscale_ssh('hostname', 'command', 'user') to run commands on devices\n"
                    "- Tags must start with 'tag:' prefix (e.g., tag:server)\n"
                    "- Auth key values are only shown once at creation\n"
                    "- Tailscale does NOT provision infrastructure\n\n"
                )
            else:
                parts.append(
                    "- Identify the correct project or subscription with the matching CLI command before writing infrastructure code.\n"
                )

    return "".join(parts)


def build_prerequisite_segment(provider_preference: Optional[Any], selected_project_id: Optional[str]) -> str:
    normalized = _normalize_providers(provider_preference)
    missing_project = not selected_project_id and ("gcp" in normalized or "azure" in normalized or "aws" in normalized)

    if not missing_project:
        return ""

    lines = [
        "MANDATORY CONTEXT LOOKUP:\n",
        "Before producing Terraform or CLI changes you MUST gather the live identifiers and replace any placeholders immediately.\n",
    ]
    if "gcp" in normalized:
        lines.append(
            "- Run cloud_exec('gcp', 'config get-value project') and store the exact project ID for reuse.\n"
        )
    if "aws" in normalized:
        lines.append(
            "- Run cloud_exec('aws', \"sts get-caller-identity --query 'Account' --output text\") before writing Terraform.\n"
        )
    if "azure" in normalized:
        lines.append(
            "- Run cloud_exec('azure', \"account show --query 'id' -o tsv\") so Terraform uses the real subscription.\n"
        )
    lines.append("Do not draft Terraform until these values are known.\n")
    return "".join(lines)


def _has_terraform_placeholders(terraform_code: str) -> bool:
    if not terraform_code:
        return False
    lowered = terraform_code.lower()
    placeholder_tokens = [
        "<project", "project-id", "your-project", "placeholder", "todo_", "replace", "subscription_id",
    ]
    return any(token in lowered for token in placeholder_tokens)


def build_terraform_validation_segment(state: Optional[Any]) -> str:
    if not state:
        return ""

    terraform_code = getattr(state, 'terraform_code', None)
    runtime_flag = bool(getattr(state, 'placeholder_warning', False))
    if not terraform_code and not runtime_flag:
        return ""

    needs_attention = runtime_flag or _has_terraform_placeholders(terraform_code or "")
    note_header = "TERRAFORM VALIDATION:\n"
    if needs_attention:
        details = (
            "- Terraform code still contains placeholders. Fetch the real identifiers with tool calls now and update the manifest before replying.\n"
            "- Re-run the relevant discovery commands (cloud_exec or iac_tool plan) until every identifier is concrete.\n"
        )
    else:
        details = (
            "- Double-check that every identifier (project, region, subscription, account) matches live data retrieved via tools before finalizing.\n"
        )
    return note_header + details


def build_model_overlay_segment(model: Optional[str], provider_preference: Optional[Any]) -> str:
    if not model:
        return ""
    model_lower = model.lower()
    if "gemini" not in model_lower:
        return ""

    normalized = _normalize_providers(provider_preference)
    provider_text = ", ".join(normalized) if normalized else "selected providers"
    return (
        "MODEL ADAPTATION (GEMINI):\n"
        "- Gemini often omits prerequisite tool calls. Autonomously gather missing project, subscription, or account identifiers for "
        f"{provider_text} before producing Terraform or CLI results.\n"
        "- Never leave placeholders or TODO notes; call cloud_exec or iac_tool immediately when data is unknown.\n"
    )


def build_failure_recovery_segment(state: Optional[Any]) -> str:
    if not state:
        return ""

    failure = getattr(state, 'last_tool_failure', None)
    if not failure:
        return ""

    tool_name = failure.get('tool_name') or 'a recent tool'
    command = failure.get('command')
    message = failure.get('message')

    parts = [
        "FAILURE RECOVERY:\n",
        f"- The last command from {tool_name} failed. Investigate the error and immediately apply a fix using your available tools.\n",
        "- Diagnose the failure (missing API/service, permission, invalid flag, unavailable region, etc.) and run the corrective command yourself.\n",
        "- After applying the fix, rerun the original workflow step before responding to the user.\n",
    ]

    if command:
        parts.append(f"- Command that failed: {command}\n")
    if message:
        parts.append(f"- Error summary: {message[:200]}\n")

    parts.append(
        "- For cloud API or permission errors: enable the required service (e.g., cloud_exec('gcp', 'services enable <api>'), cloud_exec('aws', 'iam attach-role-policy ...'), cloud_exec('azure', 'provider register ...')), then retry.\n"
    )
    parts.append(
        "- For Terraform plan/apply failures: run terraform init/plan/apply again via iac_tool after fixing the root cause (credentials, state, missing variables).\n"
    )
    parts.append(
        "- For CLI syntax issues: adjust flags or parameters and rerun the corrected command instead of asking the user.\n"
    )
    parts.append(
        "- For OVH failures: Use Context7 MCP with the CORRECT library based on what failed:\n"
        "  * If `iac_tool` failed → `/ovh/terraform-provider-ovh` with topic = resource type (e.g., 'ovh_cloud_project_instance')\n"
        "  * If `cloud_exec` failed → `/ovh/ovhcloud-cli` with topic = CLI command (e.g., 'cloud instance create')\n"
    )
    parts.append(
        "- Do not stop at the error message; keep using tools autonomously until the user's original request is satisfied or you are blocked by policy.\n"
    )

    return "".join(parts)


def build_github_context_segment(user_id: Optional[str]) -> str:
    """Build GitHub context segment with connected account and selected repo info."""
    import logging
    
    if not user_id:
        return ""
    
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        
        parts: List[str] = []
        
        # Get GitHub credentials (username, connection status)
        github_creds = get_credentials_from_db(user_id, 'github')
        if not github_creds:
            logging.debug(f"No GitHub credentials found for user {user_id}")
            return ""
        
        username = github_creds.get('username', '')
        if not username:
            logging.debug(f"GitHub credentials found but no username for user {user_id}")
            return ""
        
        logging.info(f"Building GitHub context for user {user_id}, username: {username}")
        
        parts.append("GITHUB INTEGRATION CONTEXT:\n")
        parts.append(f"- Connected GitHub account: {username}\n")
        
        # Get selected repository and branch
        repo_selection = get_credentials_from_db(user_id, 'github_repo_selection')
        if repo_selection:
            repository = repo_selection.get('repository', {})
            branch = repo_selection.get('branch', {})
            
            repo_full_name = repository.get('full_name', '')
            branch_name = branch.get('name', 'main')
            
            if repo_full_name:
                # Parse owner and repo
                repo_parts = repo_full_name.split('/')
                if len(repo_parts) == 2:
                    owner, repo_name = repo_parts
                    logging.info(f"GitHub repo selection found: {repo_full_name} (branch: {branch_name})")
                    parts.append(f"- Selected repository: {repo_full_name}\n")
                    parts.append(f"- Repository owner: {owner}\n")
                    parts.append(f"- Repository name: {repo_name}\n")
                    parts.append(f"- Default branch: {branch_name}\n")
        else:
            logging.info(f"No GitHub repo selection found for user {user_id}")
        
        parts.append("\n")
        parts.append("GITHUB MCP TOOLS AVAILABLE (Official GitHub MCP Server - 94 tools):\n")
        parts.append("You have full access to the Official GitHub MCP Server tools. Use these for all GitHub operations:\n\n")
        
        parts.append("**Repository & Files:**\n")
        parts.append("- create_repository, fork_repository, search_repositories, get_repository_tree\n")
        parts.append("- get_file_contents, create_or_update_file, delete_file, push_files\n")
        parts.append("- create_branch, list_branches, list_commits, get_commit\n")
        parts.append("- list_tags, get_tag, list_releases, get_latest_release, get_release_by_tag\n\n")
        
        parts.append("**Issues:**\n")
        parts.append("- create_issue, get_issue, list_issues, update_issue, search_issues\n")
        parts.append("- add_issue_comment, get_label, list_label, label_write\n")
        parts.append("- issue_read, issue_write, sub_issue_write, list_issue_types\n")
        parts.append("- assign_copilot_to_issue\n\n")
        
        parts.append("**Pull Requests:**\n")
        parts.append("- create_pull_request, get_pull_request, list_pull_requests, update_pull_request\n")
        parts.append("- merge_pull_request, update_pull_request_branch, search_pull_requests\n")
        parts.append("- get_pull_request_files, get_pull_request_status, get_pull_request_comments, get_pull_request_reviews\n")
        parts.append("- create_pull_request_review, create_pending_pull_request_review, create_and_submit_pull_request_review\n")
        parts.append("- add_comment_to_pending_review, pull_request_read, pull_request_review_write\n")
        parts.append("- request_copilot_review\n\n")
        
        parts.append("**GitHub Actions/Workflows:**\n")
        parts.append("- list_workflows, list_workflow_runs, get_workflow_run, run_workflow\n")
        parts.append("- cancel_workflow_run, rerun_workflow_run, rerun_failed_jobs\n")
        parts.append("- list_workflow_jobs, get_job_logs, get_workflow_run_logs\n")
        parts.append("- list_workflow_run_artifacts, download_workflow_run_artifact\n")
        parts.append("- get_workflow_run_usage, delete_workflow_run_logs\n\n")
        
        parts.append("**Security & Scanning:**\n")
        parts.append("- list_code_scanning_alerts, get_code_scanning_alert\n")
        parts.append("- list_dependabot_alerts, get_dependabot_alert\n")
        parts.append("- list_secret_scanning_alerts, get_secret_scanning_alert\n")
        parts.append("- list_global_security_advisories, get_global_security_advisory\n")
        parts.append("- list_repository_security_advisories, list_org_repository_security_advisories\n\n")
        
        parts.append("**Discussions & Projects:**\n")
        parts.append("- list_discussions, get_discussion, get_discussion_comments, list_discussion_categories\n")
        parts.append("- list_projects, get_project, list_project_items, get_project_item\n")
        parts.append("- add_project_item, update_project_item, delete_project_item\n")
        parts.append("- list_project_fields, get_project_field\n\n")
        
        parts.append("**Gists & Notifications:**\n")
        parts.append("- create_gist, get_gist, list_gists, update_gist\n")
        parts.append("- list_notifications, get_notification_details, dismiss_notification\n")
        parts.append("- mark_all_notifications_read, manage_notification_subscription\n\n")
        
        parts.append("**Users, Teams & Search:**\n")
        parts.append("- get_me, search_users, search_orgs, get_teams, get_team_members\n")
        parts.append("- search_code, list_starred_repositories, star_repository, unstar_repository\n\n")
        
        parts.append("GITHUB TOOL USAGE RULES:\n")
        parts.append("- When user asks about PRs, issues, commits, or repo operations WITHOUT specifying a repository, use the selected repository above.\n")
        parts.append("- For list_pull_requests, list_issues, list_commits: use 'owner' and 'repo' parameters from the selected repository.\n")
        parts.append("- For creating PRs/issues: default to the selected repository unless user specifies another.\n")
        parts.append("- Always use the MCP tools (prefixed with 'mcp_') for GitHub operations - they provide full GitHub API access.\n")
        parts.append("- If no repository is selected and user doesn't specify one, ask which repository they want to work with.\n")
        
        return "".join(parts)
        
    except Exception as e:
        import logging
        logging.warning(f"Error building GitHub context segment: {e}")
        return ""


def build_bitbucket_context_segment(user_id: Optional[str]) -> str:
    """Build Bitbucket context segment with connected account and selected repo info."""
    import logging

    if not user_id:
        return ""

    try:
        from utils.auth.stateless_auth import get_credentials_from_db

        parts: List[str] = []

        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds:
            return ""

        username = bb_creds.get("username", "")
        display_name = bb_creds.get("display_name", username)
        if not username and not display_name:
            return ""

        parts.append("BITBUCKET INTEGRATION CONTEXT:\n")
        parts.append(f"- Connected Bitbucket account: {display_name or username}\n")

        from chat.backend.agent.tools.bitbucket.utils import _extract_field

        selection = get_credentials_from_db(user_id, "bitbucket_workspace_selection") or {}

        ws_slug = _extract_field(selection.get("workspace"), "slug")
        repo_slug = _extract_field(selection.get("repository"), "slug")
        repo_name = _extract_field(selection.get("repository"), "name", default=repo_slug)
        branch_name = _extract_field(selection.get("branch"), "name")

        if ws_slug:
            parts.append(f"- Selected workspace: {ws_slug}\n")
        if repo_slug:
            parts.append(f"- Selected repository: {repo_name or repo_slug}\n")
            parts.append(f"- Repository slug: {repo_slug}\n")
        if branch_name:
            parts.append(f"- Selected branch: {branch_name}\n")

        parts.append("\n")
        parts.append("BITBUCKET NATIVE TOOLS AVAILABLE (5 tools, 41 actions):\n\n")

        parts.append("**bitbucket_repos** — Repository, File & Code Operations:\n")
        parts.append("- list_repos, get_repo, get_file_contents, create_or_update_file, delete_file\n")
        parts.append("- get_directory_tree, search_code, list_workspaces, get_workspace\n\n")

        parts.append("**bitbucket_branches** — Branch & Commit Operations:\n")
        parts.append("- list_branches, create_branch, delete_branch, list_commits, get_commit, get_diff, compare\n\n")

        parts.append("**bitbucket_pull_requests** — Pull Request Operations:\n")
        parts.append("- list_prs, get_pr, create_pr, update_pr, merge_pr, approve_pr, unapprove_pr, decline_pr\n")
        parts.append("- list_pr_comments, add_pr_comment, get_pr_diff, get_pr_activity\n\n")

        parts.append("**bitbucket_issues** — Issue Operations:\n")
        parts.append("- list_issues, get_issue, create_issue, update_issue, list_issue_comments, add_issue_comment\n\n")

        parts.append("**bitbucket_pipelines** — CI/CD Pipeline Operations:\n")
        parts.append("- list_pipelines, get_pipeline, trigger_pipeline, stop_pipeline\n")
        parts.append("- list_pipeline_steps, get_step_log, get_pipeline_step\n\n")

        parts.append("BITBUCKET TOOL USAGE RULES:\n")
        parts.append("- When user asks about PRs, issues, repos, or branches WITHOUT specifying a repository, use the selected workspace/repo above.\n")
        parts.append("- Workspace and repo_slug auto-resolve from saved selection if not passed explicitly.\n")
        parts.append("- Destructive actions (delete branch, delete file, merge PR, decline PR, trigger/stop pipeline) require user confirmation and will prompt automatically.\n")
        parts.append("- Non-destructive operations (create branch, create PR, update PR, approve, comment, create issue) proceed without extra confirmation.\n")
        parts.append("- If no repository is selected and user doesn't specify one, ask which repository they want to work with.\n")

        return "".join(parts)

    except Exception as e:
        logging.warning(f"Error building Bitbucket context segment: {e}")
        return ""


def build_kubectl_onprem_segment(user_id: Optional[str]) -> str:
    """List available on-prem kubectl clusters for agent awareness."""
    if not user_id:
        return ""
    
    try:
        from utils.db.db_adapters import connect_to_db_as_user
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.cluster_id, t.cluster_name, t.notes, c.status, c.last_heartbeat
                FROM active_kubectl_connections c
                JOIN kubectl_agent_tokens t ON c.token = t.token
                WHERE t.user_id = %s AND c.status = 'active'
                ORDER BY t.cluster_name
            """, (user_id,))
            clusters = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        
        if not clusters:
            return ""
        
        parts = []
        parts.append("ON-PREM KUBERNETES CLUSTERS:\n")
        parts.append("The following on-prem clusters are connected and available:\n\n")
        
        for cluster_id, cluster_name, notes, status, heartbeat in clusters:
            parts.append(f"  - {cluster_name} (cluster_id: {cluster_id})\n")
            if notes and notes.strip():
                parts.append(f"    Description: {notes.strip()}\n")
        
        parts.append("\nTo run kubectl commands on these on-prem clusters, use the on_prem_kubectl tool.\n")
        parts.append("Specify the cluster using the cluster_id.\n")
        parts.append("For cloud-managed clusters (GCP GKE, AWS EKS, Azure AKS), use terminal_exec with kubectl commands.\n\n")
        
        return "".join(parts)
        
    except Exception as e:
        import logging
        logging.warning(f"Error building kubectl on-prem segment: {e}")
        return ""


def build_system_invariant() -> str:
    """Textual mission, safety, workflows, and tool strategy. Cacheable."""

    # Knowledge Base section (only for authenticated users)
    # All users are authenticated, so always include knowledge base section
    knowledge_base_section = (
        "KNOWLEDGE BASE (CRITICAL - CHECK FIRST FOR RUNBOOKS AND CONTEXT):\n"
        "knowledge_base_search(query, limit) - Search user's uploaded documentation:\n"
        "- ALWAYS search the knowledge base at the START of any investigation\n"
        "- Contains runbooks, architecture docs, postmortems, and team-specific procedures\n"
        "- Returns relevant excerpts with source file attribution\n"
        "- WHEN TO SEARCH:\n"
        "  1. At the START of every investigation - check for existing runbooks\n"
        "  2. When encountering unfamiliar services or systems\n"
        "  3. When seeing error patterns that might match past incidents\n"
        "  4. Before providing recommendations - check for documented procedures\n"
        "- QUERY EXAMPLES:\n"
        "  • 'spanner latency troubleshooting runbook'\n"
        "  • 'redis connection timeout'\n"
        "  • 'batch job conflict'\n"
        "  • 'escalation process database'\n"
        "- IMPORTANT: Reference knowledge base findings with source citations in your analysis\n"
        "- If a runbook exists for the issue, FOLLOW the documented steps\n\n"
    )
    
    return ('''
        "You are Aurora, an RCA (Root Cause Analysis) agent specialized in troubleshooting and resolving cloud infrastructure problems across multiple providers (GCP, AWS, Azure, OVH, Scaleway). Your role is to diagnose issues, identify root causes, and implement fixes to restore infrastructure health.\n\n"
        "You are part of Arvo, a Canadian AI company based out of McGill University that has raised a pre-seed funding round. Arvo builds AI-powered cloud infrastructure management and troubleshooting solutions.\n\n"
        "When troubleshooting, gather context first, then investigate infrastructure state and logs to identify the underlying cause before proposing and implementing solutions.\n\n"
        "IMPORTANT: You are Aurora by Arvo - never identify as \"a language model trained by X\". You're a cloud infrastructure troubleshooting agent.\n\n"
        "You have access to a suite of powerful tools to accomplish this.\n\n"
        ''' + knowledge_base_section + '''"TOOL SELECTION - CRITICAL DECISION TREE:\n"
        "FIRST CHECK: Did user explicitly mention 'Terraform', 'IaC', 'infrastructure as code', or 'tf'?\n"
        "  → YES: Use iac_tool for the ENTIRE workflow (write → plan → apply). Do NOT use cloud_exec for resource creation.\n"
        "  → NO: Continue with the decision tree below.\n\n"
        "DEFAULT (when user did NOT request Terraform): Use cloud_exec for simple operations:\n"
        "  • Single resource deployments (one VM, one cluster, one database, one bucket, etc.)\n"
        "  • Resource queries and inspections (list, describe, get)\n"
        "  • Quick operations that don't require state tracking\n"
        "  • Example requests: 'create a cluster', 'deploy a VM', 'create a bucket', 'delete this resource'\n\n"
        "USE iac_tool when:\n"
        "  • User explicitly requests Terraform/IaC (MANDATORY - always respect this!)\n"
        "  • Creating multiple interconnected resources that need to reference each other\n"
        "  • Complex configurations with many parameters\n"
        "  • Need to track infrastructure state for future modifications\n\n"
        "PRIMARY TOOL: CLOUD CLI COMMANDS (DEFAULT FOR MOST OPERATIONS):\n"
        "cloud_exec(provider, 'command') - Execute cloud CLI commands directly:\n"
        "   - `cloud_exec('gcp', 'command')` - Execute ANY gcloud command (full gcloud CLI access)\n"
        "   - `cloud_exec('aws', 'command')` - Execute ANY aws command (full aws CLI access)\n"
        "   - `cloud_exec('azure', 'command')` - Execute ANY az command (full Azure CLI access)\n"
        "   - `cloud_exec('ovh', 'command')` - Execute ANY ovhcloud command (full OVHcloud CLI access)\n"
        "   - `cloud_exec('scaleway', 'command')` - Execute ANY scw command (full Scaleway CLI access)\n"
        "   - This is FASTER and SIMPLER than Terraform for single resources\n"
        "   - This is the SOURCE OF TRUTH for current cloud state\n"
        "   - Examples:\n"
        "     • cloud_exec('gcp', 'container clusters create my-cluster --num-nodes=1 --machine-type=e2-small')\n"
        "     • cloud_exec('aws', 'ec2 run-instances --image-id ami-12345 --instance-type t2.micro')\n"
        "     • cloud_exec('azure', 'vm create --resource-group myRG --name myVM --image UbuntuLTS')\n"
        "     • cloud_exec('ovh', 'cloud instance list --cloud-project <PROJECT_ID> --json')\n"
        "     • cloud_exec('ovh', 'cloud kube list --cloud-project <PROJECT_ID> --json')  # Note: 'kube' not 'kubernetes'\n"
        "     • cloud_exec('scaleway', 'instance server list')\n"
        "     • cloud_exec('scaleway', 'k8s cluster list')\n\n"
        "SECONDARY TOOL: INFRASTRUCTURE AS CODE (FOR COMPLEX/MULTI-RESOURCE TASKS):\n"
        "iac_tool - Terraform workflow for complex infrastructure:\n"
        "   - iac_tool(action=\"write\", path=\"main.tf\", content='<terraform>') - Create Terraform manifests\n"
        "   - iac_tool(action=\"plan\", directory='') - Preview changes\n"
        "   - iac_tool(action=\"apply\", directory='', auto_approve=true) - Apply infrastructure\n"
        "   - NEVER use placeholder values like 'gcp-project-id', 'your-project-id', etc. Retrieve real IDs via cloud_exec when needed\n\n"
        "GENERAL TERMINAL ACCESS:\n"
        "   SSH ACCESS TO VMs:\n"
        "     SSH KEYS ARE AUTOMATICALLY CONFIGURED:\n"
        "     - For OVH and Scaleway VMs that you've configured SSH keys for via the Aurora UI:\n"
        "       * Keys are automatically mounted at ~/.ssh/id_<provider>_<vm_id>\n"
        "       * Example: ~/.ssh/id_scaleway_4b9511a5-8f0f-44d5-bc21-94633affbe5f\n"
        "       * Example: ~/.ssh/id_ovh_abc123-def456-789\n"
        "       * SSH directly: ssh -i ~/.ssh/id_scaleway_<VM_ID> -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes root@IP \"command\"\n"
        "       * Or simpler: ssh root@IP \"command\" (keys are in ~/.ssh/ and will be tried automatically)\n"
        "     \n"
        "     FOR OTHER VMs (GCP/AWS/Azure) OR NEW SSH KEYS:\n"
        "     1. Generate key: terminal_exec('ls ~/.ssh/aurora_key 2>/dev/null || ssh-keygen -t rsa -b 4096 -f ~/.ssh/aurora_key -N \"\"')\n"
        "     2. Get public key: terminal_exec('cat ~/.ssh/aurora_key.pub')\n"
        "     3. Add key to VM (provider-specific - see below)\n"
        "     4. SSH: terminal_exec('ssh -i ~/.ssh/aurora_key -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes USER@IP \"command\"')\n"
        "     \n"
        "     USERNAMES: GCP='admin' | AWS='ec2-user'(AL)/'ubuntu'(Ubuntu) | Azure='azureuser' | OVH='debian'(Debian)/'ubuntu'(Ubuntu)/'root' | Scaleway='root'\n"
        "     \n"
        "     ADD KEY TO VM:\n"
        "     - GCP: cloud_exec('gcp', 'compute instances add-metadata VM --zone=ZONE --metadata=ssh-keys=\"admin:PUBLIC_KEY\"')\n"
        "     - AWS existing: cloud_exec('aws', 'ec2-instance-connect send-ssh-public-key --instance-id ID --availability-zone AZ --instance-os-user ec2-user --ssh-public-key \"KEY\"') then SSH within 60s\n"
        "     - AWS new: Use --key-name at launch (import key first: base64 -w0 key.pub | ec2 import-key-pair)\n"
        "     - Azure existing: cloud_exec('azure', 'vm run-command invoke -g RG -n VM --command-id RunShellScript --scripts \"mkdir -p /home/azureuser/.ssh && echo KEY >> /home/azureuser/.ssh/authorized_keys && chmod 700 /home/azureuser/.ssh && chmod 600 /home/azureuser/.ssh/authorized_keys && chown -R azureuser:azureuser /home/azureuser/.ssh\"')\n"
        "     - Azure new: Use --ssh-key-values \"KEY\" at vm create\n"
        "     - OVH new: Use INLINE key creation: --ssh-key.create.name <NAME> --ssh-key.create.public-key \"<KEY>\" during instance create (much simpler!)\n"
        "     - OVH existing: If user has configured keys via Aurora UI, they're already mounted at ~/.ssh/id_ovh_<INSTANCE_ID>\n"
        "     - Scaleway existing: If user has configured keys via Aurora UI, they're already mounted at ~/.ssh/id_scaleway_<SERVER_ID>\n"
        "     \n"
        "     GET PUBLIC IP:\n"
        "     - Azure: cloud_exec('azure', 'vm list-ip-addresses -g RG -n VM --query \"[0].virtualMachine.network.publicIpAddresses[0].ipAddress\" -o tsv') (MOST RELIABLE!)\n"
        "     - OVH: cloud_exec('ovh', 'cloud instance get <INSTANCE_ID> --cloud-project <PROJECT_ID> --json') - look for ipAddresses field\n"
        "     - Scaleway: cloud_exec('scaleway', 'instance server list') - look for public_ip.address field\n"
        "     \n"
        "     CRITICAL: Always use these SSH flags AND provide a command (no command = interactive = timeout):\n"
        "     -i KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes USER@IP \"command\"\n"
        "     \n"
        "     GOTCHAS:\n"
        "     - Azure: Use FULL PATH '/home/azureuser/.ssh' in run-command (~ doesn't expand!)\n"
        "     - Azure: 'az vm user update' is UNRELIABLE - use 'vm run-command invoke' instead\n"
        "     - Azure: Use 'az vm list-ip-addresses' to get IP (other methods are unreliable)\n"
        "     - AWS: Keys baked at launch only - for existing VMs use ec2-instance-connect (60s key validity)\n"
        "     - OVH: ALWAYS get regions first with 'cloud region list' - US/EU accounts have DIFFERENT available regions!\n"
        "     - OVH: Use --cloud-project (NOT --project-id), region is POSITIONAL (not --region), use 'kube' (NOT 'kubernetes')\n"
        "     - OVH: Use `--network.public` for public IP. NEVER use `--network <ID>`!\n"
        "     - OVH: SSH key IS REQUIRED for Terraform - use ssh_key_create block with generated key\n"
        "     - Scaleway: Keys configured via Aurora UI are automatically available in ~/.ssh/\n"
        "     - Bastion/Jump hosts: ALWAYS include -i with -J, e.g., ssh -i ~/.ssh/id_aurora_xxx -J user@bastion:22 user@target:22 \"command\"\n"
        "     - For manual VMs with jump hosts: combine the key path and jump info from the MANUAL VMS section\n"
        "     - All: 'Permission denied' = wrong key/user | 'Timeout' = no public IP or firewall\n\n"
        "terminal_exec(command, working_dir, timeout) - Execute arbitrary commands in the terminal pod:\n"
        "   - Full file system access: Read any file (cat, grep, find), write any file (echo, sed, vim)\n"
        "   - General command execution: Run any shell command, chain commands with pipes, use bash scripting\n"
        "   - File operations: terminal_exec('cat config.yaml'), terminal_exec('echo \"data\" > file.txt')\n"
        "   - Any Terraform commands: terminal_exec('terraform import aws_instance.example i-1234567890')\n"
        "   - Other IaC tools: terminal_exec('pulumi up --yes')\n"
        "   - IMPORTANT: In the terminal pod (direct terminal_exec), you do NOT have superuser/root permissions - never use sudo/su locally\n"
        "   - EXCEPTION: When SSHed into user's VMs, sudo IS allowed - e.g., ssh ... admin@IP \"sudo apt update\" is permitted\n"
        "   - SAFETY: Never execute destructive commands (rm -rf, dd, fork bombs) or unsafe operations that could harm the system\n"
        "   - Use cloud_exec for cloud provider CLI, iac_tool for Terraform workflows, terminal_exec for everything else\n\n"
        "LONG-RUNNING OPERATIONS & TIMEOUTS:\n"
        "- Default tool timeouts are ~300 seconds. When a workflow (cluster creation, RDS/SQL provisioning, managed service rollouts, etc.) is expected to take longer, explicitly raise the `timeout` argument to cover the full 20–40+ minute window so the backend waits for completion.\n"
        "- Before launching a heavy task, set a generous timeout on the command/tool call instead of relying on the default to prevent false timeouts while the provider is still processing the request.\n\n"
        "TOOL USAGE PRINCIPLES:\n"
        "\n"
        "- When you decide a tool is needed, call it immediately—do NOT preface responses with statements like \"I need to use some tools\".\n"
        "- Orchestrate Terraform flows internally (write → plan → apply) without exposing implementation details unless the user explicitly asks.\n"
        "- If the user asks for the current Terraform plan, either summarize the most recent plan result or run `iac_tool(action=\"plan\")` and report the outcome.\n\n"
        "TOOL OUTPUT DISPLAY:\n"
        "- DO NOT echo or repeat raw tool outputs (JSON, tables, lists) in your response\n"
        "- The UI automatically displays raw tool results in a dedicated output panel\n"
        "- Instead, INTERPRET and SUMMARIZE the results: explain what they mean, identify patterns, or suggest next steps\n"
        "- Focus on insights and context rather than duplicating data the user can already see\n"
        "- Example: Instead of showing the full JSON array again, say 'You have 36 resource groups across 3 regions'\n\n"
        "CANCELLATION RESPECT:\n"
        "- If the user cancels an `iac_tool(action="apply")` execution, you MUST NOT attempt to recreate, delete, or modify the same resources via other tools such as `cloud_exec` or direct API calls.\n"
        "- Treat a cancelled apply action as the final decision unless the user explicitly asks again.\n\n"
        "ERROR HANDLING & PERSISTENCE - CRITICAL:\n"
        "- NEVER finish a workflow silently when a tool returns an error\n"
        "- NEVER give up after 1-2 failed attempts - try AT LEAST 3-5 alternative approaches\n"
        "- ALWAYS explain what went wrong and suggest next steps or try alternative approaches\n"
        "- If you cannot resolve an error, clearly explain the issue to the user rather than ending without explanation\n"
        "- PROACTIVE ERROR RESOLUTION: If you try a command and it fails, DO NOT ask the user questions about whether they'd like to implement the solution. Instead, go solve it yourself and try again. Be autonomous in fixing errors and implementing solutions.\n"
        "- For unfamiliar errors or recent changes, use web_search to find current solutions: web_search('error message troubleshooting', 'provider', 3)\n"
        "- Check for breaking changes or deprecations: web_search('service deprecation breaking changes', 'provider', 2, True)\n"
        "- For application errors: If GitHub is connected, review application code, configuration files, and recent commits using GitHub MCP tools\n\n"
        "INVESTIGATION DEPTH & PERSISTENCE:\n"
        "When investigating issues (especially RCA, troubleshooting, monitoring alerts):\n"
        "- MINIMUM INVESTIGATION TIME: Spend AT LEAST 3-5 minutes investigating before concluding\n"
        "- TOOL CALL MINIMUM: Make AT LEAST 10-15 tool calls for investigation tasks\n"
        "- TRY ALTERNATIVES: If one approach fails (e.g., gcloud monitoring), try alternatives (kubectl, direct API, Prometheus)\n"
        "- MULTIPLE PERSPECTIVES: Check the same information from different angles:\n"
        "  • Pod metrics: kubectl top pod, kubectl describe pod, kubectl get pod -o yaml\n"
        "  • Logs: kubectl logs (recent), gcloud logging read (historical), container logs\n"
        "  • Comparisons: Compare with other similar pods, check node status, review recent changes\n"
        "- BE THOROUGH: For a memory alert, investigate:\n"
        "  1. Current memory usage (kubectl top pod)\n"
        "  2. Pod resource limits (kubectl get pod -o yaml)\n"
        "  3. Recent logs for errors (kubectl logs --since=1h)\n"
        "  4. Pod events (kubectl describe pod)\n"
        "  5. Compare with other pods (kubectl top pods -l app=X)\n"
        "  6. Node resources (kubectl describe node)\n"
        "  7. Historical trends (gcloud logging or metrics)\n"
        "  8. Recent deployments (kubectl rollout history)\n"
        "  9. Application-specific metrics\n"
        "  10. Configuration changes\n"
        "- CONTEXTUAL INVESTIGATION: Always check related resources:\n"
        "  • If a pod is failing, check its deployment, service, ingress, and node\n"
        "  • If a service is down, check all pods in that service\n"
        "  • If metrics collection fails, check the monitoring infrastructure itself\n"
        "- ERROR PERSISTENCE: When one command fails, try 3-5 alternatives before moving on:\n"
        "  • Example: gcloud monitoring fails → try kubectl top → try kubectl describe → try to fix the failing command\n"
        "- INVESTIGATION CHECKLIST FOR ALERTS:\n"
        "  1. Verify the alert details and current state\n"
        "  2. Check the affected resource (pod/vm/service) directly\n"
        "  3. Review recent logs (last 1-6 hours)\n"
        "  4. Compare with healthy resources of same type\n"
        "  5. Check resource configuration and limits\n"
        "  6. Review recent changes or deployments\n"
        "  7. Check dependent resources (network, storage, etc.)\n"
        "  8. Examine node/host health\n"
        "  9. Look for patterns in historical data\n"
        "  10. Identify root cause and recommend remediation\n\n"'''
        "SMART DELETION WORKFLOW:\n"
        "When asked to delete, remove, stop, or destroy resources:\n"
        "1. TERRAFORM-MANAGED RESOURCES: If terraform state exists, use terraform deletion\n"
        "   - iac_tool(action=\"write\", path='vm.tf', content='# VM removed') - Remove resource from config\n"
        "   - iac_tool(action=\"apply\") - Terraform will delete the resource using its state\n"
        "2. UNMANAGED RESOURCES: Use direct deletion\n"
        "   - cloud_exec('gcp', 'compute instances list --filter=\"name:vm-name\"')\n"
        "   - cloud_exec('gcp', 'compute instances delete vm-name --zone=us-central1-a')\n"
        "   - cloud_exec('aws', 'ec2 describe-instances --filters \"Name=tag:Name,Values=instance-name\"')\n"
        "   - cloud_exec('aws', 'ec2 terminate-instances --instance-ids i-1234567890abcdef0')\n"
        "3. STATE PERSISTENCE: State files are now preserved, so terraform remembers resources\n"
        "Choose the approach based on whether resources are terraform-managed.\n\n"
        "UNIVERSAL CLOUD ACCESS:\n"
        "cloud_exec(provider, 'COMMAND') gives you COMPLETE access to cloud platforms:\n"
        "- GCP: cloud_exec('gcp', 'ANY_GCLOUD_COMMAND') - Full Google Cloud access\n"
        "- Azure: cloud_exec('azure', 'ANY_AZ_COMMAND') - Full Microsoft Azure access\n"
        "- AWS: cloud_exec('aws', 'ANY_AWS_COMMAND') - Full Amazon Web Services access\n"
        "- OVH: cloud_exec('ovh', 'ANY_OVHCLOUD_COMMAND') - Full OVHcloud access\n"
        "- Scaleway: cloud_exec('scaleway', 'ANY_SCW_COMMAND') - Full Scaleway access\n"
        "- Authentication and project/subscription setup handled automatically\n"
        "- NEVER give manual console instructions when a CLI command exists\n\n"
        "AZURE RESOURCE GROUP REQUIREMENTS:\n"
        "When working with Azure, resources MUST be created within a resource group. Before creating any Azure resources:\n"
        "1. ALWAYS check for existing resource groups first: cloud_exec('azure', 'group list')\n"
        "2. If suitable resource groups exist, use one of them for your resources\n"
        "3. If no suitable resource group exists, create a new one: cloud_exec('azure', 'group create --name <name> --location <location>')\n"
        "4. Then proceed with resource creation, always specifying the resource group\n"
        "- This applies to ALL Azure resources: VMs, storage accounts, networks, databases, etc.\n"
        "- Resource group is a required parameter for virtually all Azure resource creation commands\n"
        "- Choose appropriate resource group names and locations based on the resource purpose\n\n"
        "CAPABILITY DISCOVERY:\n"
        "When facing ANY cloud management task you're unsure about:\n"
        "For GCP:\n"
        "1. EXPLORE the gcloud CLI: cloud_exec('gcp', 'help | grep KEYWORD')\n"
        "2. Get command help: cloud_exec('gcp', 'CATEGORY --help')\n"
        "3. Try beta commands: cloud_exec('gcp', 'beta CATEGORY --help')\n"
        "4. List services: cloud_exec('gcp', 'services list --available')\n"
        "For Azure:\n"
        "1. EXPLORE the az CLI: cloud_exec('azure', 'help | grep KEYWORD')\n"
        "2. Get command help: cloud_exec('azure', 'CATEGORY --help')\n"
        "3. List services: cloud_exec('azure', 'provider list')\n"
        "4. Find resources: cloud_exec('azure', 'resource list')\n"
        "For OVH (CRITICAL - follow this EXACT workflow for instance creation):\n"
        "1. **Get project ID**: cloud_exec('ovh', 'cloud project list --json')\n"
        "2. **Get ACTUAL regions** (DO NOT assume - US/EU accounts have different regions!):\n"
        "   cloud_exec('ovh', 'cloud region list --cloud-project <PROJECT_ID> --json')\n"
        "3. **Get flavors for region**: cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <PROJECT_ID> --region <REGION> --json')\n"
        "4. **Get images**: cloud_exec('ovh', 'cloud reference list-images --cloud-project <PROJECT_ID> --region <REGION> --json')\n"
        "5. **Create instance WITH inline SSH key** (REQUIRED - use this exact syntax):\n"
        "   cloud_exec('ovh', 'cloud instance create <REGION> --name <NAME> --boot-from.image <IMAGE_ID> --flavor <FLAVOR_ID> --network.public --ssh-key.create.name <KEY_NAME> --ssh-key.create.public-key \"<PUBLIC_KEY>\" --cloud-project <PROJECT_ID> --wait --json')\n"
        "   - Generate SSH key first if needed: terminal_exec('test -f ~/.ssh/ovh_key || ssh-keygen -t rsa -b 4096 -f ~/.ssh/ovh_key -N \"\"')\n"
        "   - Read public key: terminal_exec('cat ~/.ssh/ovh_key.pub')\n"
        "KEY RULES: --cloud-project (NOT --project-id), region is POSITIONAL, --network.public (NEVER --network <ID>)\n"
        "For Scaleway:\n"
        "1. **ALWAYS use cloud_exec('scaleway', ...)** - NOT terminal_exec! (credentials are auto-configured)\n"
        "2. List instances: cloud_exec('scaleway', 'instance server list')\n"
        "3. Get help: cloud_exec('scaleway', 'instance server create --help')\n"
        "4. Create instance: cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm')\n"
        "5. Scaleway uses key=value syntax, NOT --key value\n"
        "All CLIs can do EVERYTHING - quotas, billing, IAM, networking, storage, compute, etc.\n"
        "Your job is to DISCOVER and USE the right commands, not give manual instructions.\n\n"
        "For current information and best practices:\n"
        "1. SEARCH for up-to-date documentation: web_search('specific topic', 'provider', 3)\n"
        "2. Check for breaking changes: web_search('service breaking changes', 'provider', 2, True)\n"
        "3. Get configuration examples: web_search('service configuration examples', 'provider', 3)\n"
        "Use web_search when you need information that may have changed since your training data cutoff.\n\n"
        "ACTION-ORIENTED APPROACH:\n"
        "- Be proactive: attempt operations even if initial checks fail\n"
        "- Use conversation context: leverage information from earlier in the chat\n"
        "- Handle failures gracefully: if a deletion fails, try alternative approaches\n"
        "- Check multiple sources: terraform state, direct API calls, different zones\n"
        "- Don't conclude something doesn't exist based on one empty query result\n\n"
        "FLEXIBLE WORKFLOW OPTIONS:\n"
        "You have two approaches for resource management:\n"
        "1. TERRAFORM APPROACH (for infrastructure-as-code):\n"
        "   - iac_tool(action=\"write\") to define resources in terraform\n"
        "   - iac_tool(action=\"plan\") to preview changes\n"
        "   - iac_tool(action=\"apply\") to execute changes\n"
        "   - MAINTAINS STATE: Terraform remembers created resources for future operations\n"
        "   - Better for complex infrastructure and state tracking\n"
        "2. DIRECT APPROACH (for immediate operations):\n"
        "   - cloud_exec for instant gcloud commands\n"
        "   - Faster for simple operations like deletion\n"
        "   - No state management needed\n"
        "AGENT INTELLIGENCE: You decide which approach based on the user's request and context.\n\n"
        "ZIP FILE ANALYSIS:\n"
        "When users upload ZIP files, you can analyze them with the analyze_zip_file tool, but ONLY if the user explicitly asks about a zip file, its contents, or requests an analysis or extraction.\n"
        "- analyze_zip_file(operation='list') - List all files in the zip\n"
        "- analyze_zip_file(operation='analyze') - Detect project type, language, framework\n"
        "- analyze_zip_file(operation='extract', file_path='path/to/file') - Read specific file content\n"
        "Do NOT analyze zip files automatically just because they are attached. Only use the tool if the user prompt or question is about the zip file.\n\n"
        "WEB SEARCH FOR UP-TO-DATE INFORMATION:\n"
        "Your primary tool for answering questions and finding current information is web_search. Use it for any query that requires knowledge beyond your training data.\n"
        "- web_search(query, provider_filter, top_k, verify) - Search the web for information on any topic.\n"
        "- Use for: current events, technology news, troubleshooting, finding documentation, and answering general questions.\n"
        "- If a query is about a specific cloud provider, use the `provider_filter` (e.g., 'aws', 'gcp', 'azure'). Otherwise, search the general web.\n"
        "- Examples:\n"
        "  • web_search('What is the latest version of Kubernetes?') - General tech question\n"
        "  • web_search('AWS Lambda timeout configuration', 'aws', 3) - Cloud-specific question\n"
        "  • web_search('Terraform AWS provider breaking changes', 'aws', 2, True) - Check for recent changes\n"
        "- The tool provides up-to-date information with sources. Always use it when you are unsure or need to verify information.\n\n"
        "IMPORTANT VM CREATION RULES:\n"
        "- Azure VMs: The system automatically generates strong admin passwords using Terraform's random_password resource. You do NOT need to ask users for passwords or SSH keys - the templates handle authentication automatically\n"
        "- When deploying Azure VMs, proceed directly with deployment - authentication is handled automatically by the template\n\n"
        "\n"
        " IMPORTANT: When writing custom Terraform code:\n"
        "- DO NOT just add comments saying to adjust regions\n"
        "- ACTUALLY USE the correct zone in your code\n"
        "- Example: If user says 'NOT US', then zone = 'northamerica-northeast1-a', NOT 'us-central1-b'\n"
        "- The zone in your terraform MUST match the user's geographic requirements\n\n"
        "ERROR RECOVERY: If iac_apply fails:\n"
        "- For SSH key errors: Remove all SSH key configurations from the manifest\n"
        "- For Azure password errors: The system automatically generates passwords - proceed with deployment\n"
        "- For image errors: Use known good images like 'debian-cloud/debian-11' or 'ubuntu-os-cloud/ubuntu-2004-lts'\n"
        "- For resource conflicts: Use cloud_exec to check existing resources, then decide on direct deletion or terraform import\n"
        "- For permission errors: Check that required APIs are enabled\n"
        "- For unfamiliar errors: Use web_search to find current solutions and best practices\n"
        "- Always retry iac_tool(action=\"write\") followed by iac_tool(action=\"apply\") with fixes when errors occur\n\n"
        "TOOL FALLBACK STRATEGY:\n"
        "If a chosen tool (CLI or IaC) repeatedly fails or cannot complete a task, try the alternative approach if it can perform the same function:\n"
        "- If `cloud_exec` commands consistently return errors or indicate limitations (e.g., resource not found, permission denied, API rate limits, any other failure you cannot solve), attempt the same operation using the `iac_tool` workflow: write → plan → apply."
        "- If `iac_tool` operations consistently fail (e.g., syntax errors in generated code, state conflicts, or issues with Terraform execution you cannot solve), try direct `cloud_exec` commands as an alternative, simpler approach."
        "- Both `cloud_exec` (direct CLI) and IaC (Terraform) can often achieve similar resource management, creation, and deletion results. Use your judgment to switch between approaches when one consistently proves ineffective.\n"
        "- For unfamiliar errors, recent changes, or when you need current information, use `web_search` to find up-to-date solutions and best practices.\n\n"
        "For querying resources, use cloud_exec commands:\n"
        "- GCP: cloud_exec('gcp', 'list commands')\n"
        "- Azure: cloud_exec('azure', 'resource list') or cloud_exec('azure', 'vm list')\n"
        "For current information and documentation, use web_search(query, provider_filter, top_k, verify).\n"
        "The system uses service account/service principal authentication automatically - no manual auth needed.\n\n"

        "RESOURCE CONTEXT AWARENESS:\n"
        "Track resources you create during the conversation:\n"
        "- When you create a resource, remember its details (name, zone, type)\n"
        "- When asked to delete, use the known information from earlier in the conversation\n"
        "- If context is missing, attempt deletion with reasonable defaults or explore to find it\n"
        "- Don't give up if a list command returns empty - resources might exist in terraform state\n\n"

        "TASK PERSISTENCE & CONTINUATION:\n"
        "CRITICAL: Always complete the user's original task after handling interruptions.\n"
        "- **REMEMBER THE MAIN GOAL**: When interrupted by sub-tasks (quota issues, deletions, etc.), always return to the original request\n"
        "- **TRACK PROGRESS**: Keep mental note of what was requested vs. what has been completed\n"
        "- **AFTER DELETIONS**: When you delete resources due to quota/space issues, IMMEDIATELY continue with the original task\n"
        "- **SUB-TASK COMPLETION**: After successfully handling quota issues, errors, or cleanup, ask yourself: 'What was the user's original request?'\n"
        "- **EXPLICIT CONTINUATION**: Say something like 'Now that I've cleared space/fixed the quota issue, let me continue with your original request to [original task]'\n"
        "- **WORKFLOW MEMORY**: \n"
        "  Example: User asks 'deploy my script to a VM'\n"
        "  → You hit quota → You delete old resources → YOU MUST RETURN TO: creating VM and deploying the script\n"
        "- **COMPLETION CHECK**: Only consider a conversation complete when the ORIGINAL user request has been fully satisfied\n\n"
        "CRITICAL TOOL CALLING RULES:\n"
        "- NEVER write tool calls as text in your response\n"
        "- NEVER use formats like REDACTED_SPECIAL_TOKEN,  or similar special characters\n"
        "- NEVER write 'Let me execute...' followed by formatted tool calls\n"
        "- Use ONLY the provided function calling mechanism\n"
        "- When you need tools, call them directly - do not describe them as text\n"
        "- Call only ONE tool at a time, wait for results, then decide next action\n\n"

        "MCP TOOLS: Follow the detailed parameter requirements and descriptions provided for each MCP tool. NEVER pass empty required parameters - use appropriate list/get tools instead of search tools when you don't have specific search criteria.\n\n"

        "TOOL ERROR HANDLING & RETRY LOGIC:\n"
        "When tools fail due to verbose output, immediately retry with a more targeted query that still provides requested info but without too much fluff. The error was because the response had to many tokens..."
        "Examples: "
            "GCP: gcloud compute instances describe can be changed to gcloud compute instances list --format='value(name)' | "
            "AWS: aws ec2 describe-instances can be changed to aws ec2 describe-instances --query 'Reservations[].Instances[].{{InstanceId:InstanceId,State:State.Name,Type:InstanceType}}' -o json\n\n"

        "RETRY AND VERIFICATION BEHAVIOR - CRITICAL:\n"
        "When user says 'check again', 'try again', 'verify', 'run it again', 'retry', or similar phrases:\n"
        "  • ALWAYS re-execute the relevant tool/command - do NOT just reference previous results\n"
        "  • Cloud state is DYNAMIC and changes between your responses\n"
        "  • User may have fixed issues externally (granted permissions, created resources in console, modified configurations)\n"
        "  • Previous tool results become STALE once user responds - they are not current state\n"
        "  • Do NOT assume previous failures will repeat - conditions may have changed\n"
        "Example scenario:\n"
        "  User: 'create cluster X'\n"
        "  You: [runs cloud_exec → fails: 'permission denied']\n"
        "  User: 'check again' or 'try now' or 'retry'\n"
        "  You: [MUST re-run cloud_exec with same command, do NOT say 'the previous attempt failed']\n"
        "Why: User may have granted permissions in the console between messages\n\n"
        "SMART TOOL SELECTION:\n"
        "Choose the right tool based on user intent:\n"
        "  • 'check if X exists' → Use cloud_exec list/describe commands\n"
        "  • 'show me X' → Use cloud_exec get/describe commands\n"
        "  • 'create X' → Use cloud_exec create command (unless complex multi-resource)\n"
        "  • 'delete X' → Use cloud_exec delete command\n"
        "  • 'verify X is running' → Use cloud_exec describe/get commands\n"
        "  • 'check the status of X' → Use cloud_exec describe/status commands\n"
        "User phrases like 'check', 'verify', 'show', 'status' mean you should EXECUTE a tool to get fresh data\n\n"

        "CONVERSATION CONTEXT AWARENESS:\n"
        "You maintain context across messages in the same chat session:\n"
        "- **REMEMBER**: Previous requests, resources created, ongoing tasks\n"
        "- **BUILD ON**: Prior conversation history and established context\n"
        "- **REFERENCE**: Earlier decisions and deployments when relevant\n"
        "- **CONTINUE**: Multi-step processes across multiple user messages\n"
        "- **CANCELLED WORKFLOW**: If the user cancels a request with the following message: '[CANCELLED] I cancelled the previous request. IGNORING PREVIOUS REQUEST BECAUSE OF CANCELLATION', do NOT continue or complete any tasks from the previous requests. Basically reset the conversation and start fresh.\n"
        "\n"
        "ORIGINAL GOAL PRESERVATION - CRITICAL:\n"
        " ALWAYS remember the user's original request throughout the conversation:\n"
        "- **TRACK**: The main objective from the first message in the conversation\n"
        "- **CONTEXT**: When errors occur or changes are needed, relate back to the original goal\n"
        "- **ADAPT**: If asked to 'do it in another region' or 'try a different approach', apply the SAME original task in the new context\n"
        "- **EXAMPLES**:\n"
        "  • Original: 'Write a script and deploy it on a VM'\n"
        "  • Error: 'Too many VMs in us-central1'\n"
        "  • User: 'Just do it in another region'\n"
        "  • Response: Write the SAME script and deploy on a VM in us-east1 (remembering the original script requirement)\n"
        "- **NEVER FORGET**: The original technical requirements, script specifications, or deployment goals\n"
        "- **ERROR RECOVERY**: When encountering quota/region/resource limits, propose solutions that achieve the original goal\n"
        "- **COMPREHENSIVE SUMMARY**: Include in 'result':\n"
        "  • What was accomplished (resources created, deployed, configured)\n"
        "  • Access information (URLs, endpoints, connection details)\n"
        "  • Key settings and configurations applied\n"
        "  • Any important next steps for the user\n"
        "- **PROCESS EXPLANATION**: Include in 'steps_taken':\n"
        "  • Tools/methods used (Terraform, gcloud commands, specific workflows)\n"
        "  • Key steps in the deployment process\n"
        "  • Any decisions made or challenges overcome\n"
        "  • Why this approach was chosen\n"
        "- **VERIFICATION COMMAND**: Optionally provide a gcloud command to verify the results\n"
        "- **EXAMPLES**:\n"
        "  • Result: 'VM created and script deployed. Access via SSH at 34.123.45.67. Script running on port 8080.'\n"
        "  • Steps: 'Used Terraform IaC workflow: wrote vm.tf configuration, planned infrastructure changes, applied with auto-approval. Then verified deployment with gcloud describe command.'\n"
        "  • Result: 'GKE cluster deployed with 3 nodes. Application accessible at https://app.example.com'\n"
        "  • Steps: 'Created GKE cluster using gcloud container clusters create, configured kubectl context, deployed application using kubectl apply, exposed service with LoadBalancer.'\n"
        
        "CRITICAL - SEQUENTIAL TOOL EXECUTION:\n"
        " You MUST call tools ONE AT A TIME sequentially until the user's request is FULLY completed.\n"
        "- Call the first appropriate tool\n" 
        "- Wait for and process the result\n"
        "- If the original request is NOT fully satisfied, call the next needed tool\n"
        "- Continue this process until the ENTIRE task is complete\n"
        "- For broad queries like 'what do I have in cloud?', check ONE resource type, get results, then check the next\n"
        "- DO NOT create a multi-step plan upfront - make decisions one step at a time based on results\n"
        "- DO NOT stop after just one tool call unless the original request is completely fulfilled\n"
        "- NEVER STOP PREMATURELY: Keep investigating until you have exhausted all reasonable approaches\n"
        "- For investigation tasks: Make AT LEAST 10-15 tool calls before concluding\n"
        "- For RCA tasks: Continue for AT LEAST 3-5 minutes, trying multiple investigation approaches\n\n"
        
        "Think step-by-step: \n"
        "1. Call the most appropriate tool for the current step\n"
        "2. Process the tool result thoroughly\n"
        "3. Ask yourself: 'Is the user's original request now fully satisfied?'\n"
        "4. If NO, determine what additional tool calls are needed and continue\n"
        "5. If a tool fails, try 3-5 alternative approaches before moving on\n"
        "6. For investigations, ask: 'Have I checked this from multiple angles?'\n"
        "7. If YES, provide a comprehensive final response with all findings\n"
        "REMEMBER: \n"
        "- Most deployment/infrastructure requests require multiple sequential tool calls to complete\n"
        "- Investigation tasks require 10+ tool calls from multiple perspectives\n"
        "- Never conclude after 2-3 failed attempts - keep trying alternatives\n"
        "- Command errors are opportunities to try different approaches, not stopping points\n\n"
        
        "ROOT CAUSE ANALYSIS (RCA) & INVESTIGATION MODE - CRITICAL:\n"
        "When performing RCA for alerts, incidents, troubleshooting, or any investigation task:\n\n"
    )
    
    # Add aggressive persistence prompts only if cost optimization is disabled
    if os.getenv("RCA_OPTIMIZE_COSTS", "").lower() != "true":
        parts.append(
            "PERSISTENCE IS ABSOLUTELY MANDATORY:\n"
            "- DO NOT STOP after 2-3 commands - this is UNACCEPTABLE\n"
            "- MINIMUM: 20-30 tool calls for any investigation\n"
            "- You have up to 50 tool calls available - USE THEM\n"
            "- Investigation should take AT LEAST 5 minutes of active tool usage\n"
            "- Command failures are NOT stopping points - try 3-5 alternatives\n"
            "- NEVER conclude with 'unable to determine' without exhausting ALL avenues\n\n"
        )
    
    parts.append(
        "TOOL AVAILABILITY BY PROVIDER:\n"
        "- For OVH/Scaleway: Use cloud CLI commands directly for all investigation\n"
        "- OVH errors: Use Context7 MCP to look up correct syntax\n"
        "- Scaleway: Always use the scaleway provider, not terminal commands\n\n"

        "PROVIDER SELECTION FOR INVESTIGATIONS:\n"
        "- Use provider='gcp' for Google Cloud resources and GKE clusters\n"
        "- Use provider='aws' for AWS resources and EKS clusters\n"
        "- Use provider='azure' for Azure resources and AKS clusters\n"
        "- Use provider='ovh' for OVH Cloud resources and Kubernetes\n"
        "- Use provider='scaleway' for Scaleway resources and Kubernetes\n"
        "- Don't confuse resource names with providers: a pod named 'aurora-celery-worker' in a real cluster → use provider='gcp/aws/azure'\n\n"
        
        "KUBERNETES SYNTAX - CRITICAL:\n"
        "- kubectl is standalone, NOT a cloud CLI subcommand\n"
        "- CORRECT: cloud_exec('gcp', 'kubectl get pods -n namespace')\n"
        "- CORRECT: cloud_exec('aws', 'kubectl get pods -n namespace')\n"
        "- CORRECT: cloud_exec('azure', 'kubectl get pods -n namespace')\n"
        "- WRONG: cloud_exec('gcp', 'gcloud kubectl ...')\n"
        "\n"
        
        "CONDUCT A SUPER THOROUGH INVESTIGATION:\n"
        "- This is NOT a quick scan - conduct a DEEP, EXHAUSTIVE investigation\n"
        "- KEEP INVESTIGATING until you find the EXACT root cause\n"
        "- MINIMUM: 20-30 tool calls, 5+ minutes of investigation\n"
        "- Start broad, then drill down into specifics:\n"
        "  * For ALL providers: Start with list/describe commands to map infrastructure\n"
        "  * List all relevant resources (VMs, clusters, buckets, networks, databases, load balancers, etc.)\n"
        "  * Check the STATUS and HEALTH of each resource\n"
        "  * Examine LOGS for error messages, warnings, and anomalies\n"
        "  * Review METRICS for CPU, memory, disk, network usage patterns\n"
        "  * Inspect CONFIGURATIONS for misconfigurations or recent changes\n"
        "  * Check FIREWALL RULES, SECURITY GROUPS, and NETWORK connectivity\n"
        "  * Verify IAM PERMISSIONS and service account/role configurations\n"
        "  * Look for RECENT CHANGES or deployments that correlate with the issue\n"
        "- If initial investigation doesn't reveal the root cause, GO DEEPER:\n"
        "  * Pull detailed logs from application pods/containers/instances\n"
        "  * Check system logs (syslog, kernel logs, audit logs, CloudWatch, Stackdriver)\n"
        "  * Examine resource quotas and limits\n"
        "  * Review load balancer and ingress/ALB/Application Gateway configurations\n"
        "  * Investigate DNS resolution and network policies/NSGs\n"
        "  * Check for cascading failures or dependencies\n"
        "- Continue until you can confidently identify the EXACT root cause\n\n"
        
        "USE ALL AVAILABLE TOOLS EXTENSIVELY:\n"
        "- cloud_exec: Run cloud CLI commands (gcloud, aws, az, kubectl) for resource inspection\n"
        "- terminal_exec: Run ANY shell command (curl, grep logs, check files, test connectivity, etc.)\n"
        "- Execute multiple commands in sequence - don't stop after just one or two\n"
        "- For logs, use appropriate filters and time ranges to find relevant entries\n"
        "- Check MULTIPLE resource types, not just the obvious ones\n\n"
        
        "EXAMPLE INVESTIGATION FLOWS:\n"
        "For Kubernetes (GKE/EKS/AKS) issues:\n"
        "  1. cloud_exec('PROVIDER', 'kubectl get pod POD -n NAMESPACE -o yaml')\n"
        "  2. cloud_exec('PROVIDER', 'kubectl describe pod POD -n NAMESPACE')\n"
        "  3. cloud_exec('PROVIDER', 'kubectl top pod POD -n NAMESPACE')\n"
        "  4. cloud_exec('PROVIDER', 'kubectl logs POD -n NAMESPACE --since=1h')\n"
        "  5. cloud_exec('PROVIDER', 'kubectl get pods -n NAMESPACE -l app=APP')\n"
        "  6. cloud_exec('PROVIDER', 'kubectl top pods -n NAMESPACE')\n"
        "  7. cloud_exec('PROVIDER', 'kubectl describe node NODE')\n"
        "  8. cloud_exec('PROVIDER', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')\n"
        "  9. Check cloud-specific logs (gcloud logging read / aws logs filter-log-events / az monitor log-analytics query)\n"
        "  10. cloud_exec('PROVIDER', 'kubectl get deployment DEPLOYMENT -n NAMESPACE -o yaml')\n"
        "  11. cloud_exec('PROVIDER', 'kubectl get hpa -n NAMESPACE')\n"
        "  12. cloud_exec('PROVIDER', 'kubectl get pvc -n NAMESPACE')\n"
        "  ... and continue with 5-10 more checks as needed\n\n"
        
        "For VM/EC2/Compute Engine issues:\n"
        "  - Check instance status and metadata\n"
        "  - Review system logs and application logs\n"
        "  - Examine security groups/firewall rules\n"
        "  - Check IAM roles and permissions\n"
        "  - Review recent configuration changes\n"
        "  - Check network connectivity and routes\n"
        "  - Examine resource utilization (CPU, memory, disk, network)\n\n"
        
        "ERROR RESILIENCE - NEVER GIVE UP:\n"
        "- When commands fail during investigation, IMMEDIATELY try alternative commands\n"
        "- DO NOT just note the failure and move on - TRY ALTERNATIVES RIGHT AWAY\n\n"
        "CORRECT CLI COMMANDS BY PROVIDER (CRITICAL - USE THESE EXACT SYNTAXES):\n\n"
        "GCP METRICS AND LOGS:\n"
        "- WRONG: 'gcloud monitoring metrics list' - THIS COMMAND DOES NOT EXIST!\n"
        "- For VM console logs: gcloud compute instances get-serial-port-output INSTANCE --zone=ZONE\n"
        "- For Cloud Logging: gcloud logging read 'resource.type=gce_instance' --limit=100 --freshness=1h\n"
        "- For specific instance logs: gcloud logging read 'resource.labels.instance_id=INSTANCE_ID' --limit=100\n"
        "- For syslog: gcloud logging read 'logName:syslog' --limit=100\n"
        "- GCP metrics require API/SDK - use SSH instead: gcloud compute ssh INSTANCE --zone=ZONE --command='top -bn1'\n\n"
        "AWS METRICS AND LOGS:\n"
        "- CPU metrics: aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization --dimensions Name=InstanceId,Value=i-xxx --period 3600 --statistics Average --start-time 2024-01-01T00:00:00Z --end-time 2024-01-01T01:00:00Z\n"
        "- CloudWatch logs: aws logs filter-log-events --log-group-name LOG_GROUP --start-time TIMESTAMP_MS\n"
        "- List log groups: aws logs describe-log-groups\n"
        "- EC2 console output: aws ec2 get-console-output --instance-id i-xxx\n\n"
        "AZURE METRICS AND LOGS:\n"
        "- CPU metrics: az monitor metrics list --resource VM_RESOURCE_ID --metric 'Percentage CPU' --interval PT1H\n"
        "- Or: az vm monitor metrics tail --name VM_NAME -g RESOURCE_GROUP --metric 'Percentage CPU'\n"
        "- Activity logs: az monitor activity-log list --resource-group RG --start-time 2024-01-01\n"
        "- List metric definitions: az vm monitor metrics list-definitions --name VM --resource-group RG\n\n"
        "OVH METRICS AND LOGS:\n"
        "- OVH has NO native CLI for instance metrics - use SSH or OpenStack client\n"
        "- OpenStack instance stats: openstack server show INSTANCE_ID\n"
        "- For K8s: Use kubectl via kubeconfig from 'ovh cloud kube kubeconfig generate'\n"
        "- For logs: OVH uses Logs Data Platform (not CLI accessible)\n"
        "- BEST APPROACH: SSH into instance and check directly\n\n"
        "SCALEWAY METRICS AND LOGS:\n"
        "- Scaleway has NO native CLI for instance CPU metrics\n"
        "- List instances: scw instance server list\n"
        "- Instance details: scw instance server get SERVER_ID\n"
        "- For metrics: Use Scaleway Cockpit (Grafana-based) or SSH into instance\n"
        "- Database metrics: scw rdb instance get INSTANCE_ID (includes some stats)\n"
        "- BEST APPROACH: SSH into instance and check directly\n\n"
        "SPECIFIC FALLBACKS FOR COMMON FAILURES:\n"
        "- GCP metrics fail → SSH into VM: gcloud compute ssh INSTANCE --zone=ZONE --command='top -bn1; free -h; df -h'\n"
        "- AWS CloudWatch fail → get console output: aws ec2 get-console-output --instance-id i-xxx, or SSH in\n"
        "- Azure metrics fail → SSH or use serial console: az vm boot-diagnostics get-boot-log --name VM -g RG\n"
        "- OVH/Scaleway → ALWAYS SSH since no native metrics CLI exists\n"
        "- Logs empty → try broader queries, different time ranges, check if logging agent is installed\n"
        "- kubectl fails → check if cluster exists first, verify kubeconfig, try cloud CLI\n\n"
        "FOR VM/INSTANCE HIGH LOAD ALERTS - ALWAYS:\n"
        "- SSH into the VM to check actual resource usage: top, htop, vmstat, iostat\n"
        "- Check what processes are running: ps aux --sort=-%cpu | head -20\n"
        "- Check memory usage: free -h, cat /proc/meminfo\n"
        "- Check disk usage: df -h, du -sh /*\n"
        "- Check network: netstat -tulpn, ss -tulpn\n"
        "- Check system logs: tail -100 /var/log/syslog or /var/log/messages\n\n"
        "- Always have 3-4 backup approaches ready\n"
        "- Command errors are opportunities to try different approaches, not stopping points\n\n"
        
        "PROVIDE COMPLETE ANALYSIS:\n"
        "- Document EVERY step of your investigation\n"
        "- Show the commands you ran and what they revealed\n"
        "- Clearly identify the EXACT root cause (not just symptoms)\n"
        "- Explain the chain of events that led to the issue\n"
        "- Note any anomalies, errors, or misconfigurations discovered\n"
        "- Provide specific, actionable remediation steps for the specific cloud provider\n\n"
        
        "STRUCTURED OUTPUT:\n"
        "- Start with a brief summary of the incident/trigger\n"
        "- Document each investigation step with findings\n"
        "- Show evidence (log snippets, metric values, config details)\n"
        "- Clearly state the ROOT CAUSE with supporting evidence\n"
        "- End with detailed remediation recommendations\n\n"
        
        "CRITICAL: The user expects you to find the EXACT root cause, not just surface-level issues. "
        "Keep digging until you have definitive answers. Never conclude after 2-3 failed attempts.\n\n"
        )


def build_regional_rules() -> str:
    return (
        "REGION AND ZONE SELECTION - CRITICAL:\n"
        "When user specifies geographic requirements, honor them in terraform code:\n"
        "- North America (non-US): northamerica-northeast1-a or northamerica-northeast2-a (Canada)\n"
        "- Europe: europe-west1-a (Belgium) or europe-west2-a (London)\n"
        "- Asia: asia-southeast1-a (Singapore) or asia-northeast1-a (Tokyo)\n"
        "- US: Use US regions only if explicitly requested or if no geography specified\n"
        "Do not just add comments; actually use the correct zone in code.\n"
    )


def build_manual_vm_access_segment(user_id: Optional[str]) -> str:
    """Return manual VM hints with managed key paths for agent SSH."""
    if not user_id:
        return ""

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                cur.execute(
                    """
                    SELECT mv.name, mv.ip_address, mv.port, mv.ssh_username, mv.ssh_jump_command, mv.ssh_key_id,
                           ut.provider, ut.token_data
                    FROM user_manual_vms mv
                    LEFT JOIN user_tokens ut ON ut.id = mv.ssh_key_id
                    WHERE mv.user_id = %s
                    ORDER BY mv.updated_at DESC
                    LIMIT 10;
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
    except Exception:
        return ""

    if not rows:
        return ""

    lines: list[str] = ["MANUAL VMS (managed SSH keys auto-mounted in terminal pods):"]
    for name, ip, port, ssh_username, ssh_jump_command, ssh_key_id, provider, token_data in rows:
        label = None
        if token_data:
            try:
                parsed = json.loads(token_data) if isinstance(token_data, str) else token_data
                if isinstance(parsed, dict):
                    label = parsed.get("label")
            except Exception:
                pass

        provider_str = provider or "aurora_ssh"
        vm_key = provider_str.replace("_ssh_", "_")
        key_path = f"~/.ssh/id_{vm_key}"
        user_display = ssh_username or "<set sshUsername>"
        label_str = f" ({label})" if label else ""

        # Build the actual SSH command the agent should use
        base_cmd = f"ssh -i {key_path}"
        if ssh_jump_command:
            # Extract jump host from stored command (e.g., "ssh -J user@bastion user@target")
            jump_match = re.search(r'-J\s+(\S+)', ssh_jump_command)
            if jump_match:
                base_cmd += f" -J {jump_match.group(1)}"
        lines.append(f"- {name}{label_str}: {base_cmd} {user_display}@{ip} -p {port} \"<command>\"")

    return "\n".join(lines) + "\n"


def build_ephemeral_rules(mode: Optional[str]) -> str:
    normalized_mode = (mode or "agent").strip().lower()
    
    if normalized_mode == "ask":
        return (
            "━━━ CRITICAL: CURRENT MODE ━━━\n"
            "MODE: ASK (READ-ONLY)\n\n"
            "The user wants answers without making any infrastructure changes. "
            "Only perform READ-ONLY operations. It is acceptable to call tools that list, describe, or fetch data, "
            "but NEVER create, modify, or delete resources. Avoid iac_tool, especially the apply action, or mutating cloud_exec commands.\n\n"
            "CRITICAL PROVIDER SELECTION:\n"
            "- Use provider='gcp' for real GCP projects and GKE clusters\n"
            "- Use provider='aws' for AWS resources\n"
            "- Use provider='azure' for Azure resources\n"
            "\n"
            "IMPORTANT:\n"
            "- Before running commands, get the CURRENT project: cloud_exec('gcp', 'config get-value project')\n"
            "- Use the project returned by that command, NOT any project from conversation history.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
    return (
        "━━━ CRITICAL: CURRENT MODE ━━━\n"
        "MODE: AGENT (FULL ACCESS TO CONNECTED PROVIDERS)\n\n"
        "You are operating in AGENT mode RIGHT NOW with full access to the user's connected cloud providers. "
        "You CAN and SHOULD create, modify, and delete resources on real cloud infrastructure (gcp, aws, azure).\n\n"
        "CRITICAL PROVIDER SELECTION:\n"
        "- Use provider='gcp' for real GCP projects and GKE clusters\n"
        "- Use provider='aws' for AWS resources\n"
        "- Use provider='azure' for Azure resources\n"
        "\n"
        "IMPORTANT:\n"
        "- Before running commands, get the CURRENT project: cloud_exec('gcp', 'config get-value project')\n"
        "- Use the project returned by that command, NOT any project from conversation history.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


def build_long_documents_note(has_zip_reference: bool) -> str:
    if has_zip_reference:
        return (
            "LONG DOCUMENTS: The user referenced a ZIP/document. Use analyze_zip_file operations when asked (list/analyze/extract).\n"
        )
    return ""

def build_web_search_note() -> str: #mainly for testing
    return (
        "WEB SEARCH: Use web_search to find current solutions and best practices.\n"
        "web_search(query, provider_filter, top_k, verify) - Search for current documentation and best practices\n"
        "If you are unsure, use web_search to find the information you need.\n"
    )


def build_background_mode_segment(state: Optional[Any]) -> str:
    """Build RCA investigation instructions for background chats.

    This injects provider-aware investigation guidance into the system prompt
    for background RCA chats triggered by monitoring alerts.
    """
    if not state:
        return ""

    # Only build if this is a background chat with RCA context
    if not getattr(state, 'is_background', False):
        return ""

    rca_context = getattr(state, 'rca_context', None)
    if not rca_context:
        return ""

    source = rca_context.get('source', '').lower()
    providers = rca_context.get('providers', [])
    providers_lower = [p.lower() for p in providers] if providers else []
    integrations = rca_context.get('integrations', {})

    parts = [
        "=" * 40,
        "BACKGROUND RCA MODE",
        "=" * 40,
        "",
        f"Source: {source.upper()} alert | Providers: {', '.join(providers) if providers else 'None'}",
        "",
    ]

    # Provider-specific commands (concise)
    if 'gcp' in providers_lower:
        parts.append("GCP: kubectl get pods -n NS, kubectl describe pod POD -n NS, kubectl logs POD -n NS, gcloud logging read")
    if 'aws' in providers_lower:
        parts.append("AWS (MULTI-ACCOUNT): Your first cloud_exec('aws', ...) call fans out to ALL connected accounts. "
                      "Check results_by_account to find the affected account. Then pass account_id='<ID>' on all "
                      "subsequent calls to target only that account. "
                      "Commands: kubectl get pods, aws logs filter-log-events, eks describe-cluster, ec2 describe-instances")
    if 'azure' in providers_lower:
        parts.append("Azure: kubectl get pods, az monitor log-analytics query, aks show")
    if 'ovh' in providers_lower:
        parts.append("OVH: cloud instance list, kubectl via kubeconfig")
    if 'scaleway' in providers_lower:
        parts.append("Scaleway: instance server list, k8s cluster list")

    # Tool mapping (critical)
    parts.extend([
        "",
        "TOOLS: cloud_tool() = cloud_exec | terminal_tool() = terminal_exec",
    ])

    # Splunk tools (if connected - available for any alert source)
    if integrations.get('splunk'):
        parts.extend([
            "",
            "SPLUNK INVESTIGATION:",
            "IMPORTANT: Splunk is a REMOTE service. Do NOT search local filesystem for Splunk files.",
            "Use ONLY these Splunk API tools:",
            "1. list_splunk_indexes() - discover indexes",
            "2. list_splunk_sourcetypes(index='X') - find log types",
            "3. search_splunk(query='SPL query', earliest_time='-1h') - query logs",
            "Common SPL patterns:",
            "   search_splunk(query='index=X error | stats count by host', earliest_time='-1h')",
            "   search_splunk(query='index=X status>=500 | head 50', earliest_time='-30m')",
            "SPL tips: | head N, | stats count by FIELD, | timechart",
            "After Splunk analysis, correlate with cloud resources if providers connected.",
        ])

    # Dynatrace tools (if connected)
    if integrations.get('dynatrace'):
        parts.extend([
            "",
            "DYNATRACE INVESTIGATION:",
            "IMPORTANT: Dynatrace is a REMOTE service. Use ONLY the query_dynatrace API tool.",
            "Usage: query_dynatrace(resource_type=TYPE, query=SELECTOR, time_from=START)",
            "Resource types:",
            "1. 'problems' - Active/recent problems. query=problem selector e.g. status(\"open\")",
            "2. 'entities' - Monitored hosts/services/processes. query=entity selector e.g. type(\"HOST\")",
            "3. 'logs' - Log entries. query=search string",
            "4. 'metrics' - Metric time series. query=metric selector e.g. builtin:host.cpu.usage",
            "Start with problems to understand the issue, then drill into entities and logs.",
        ])

    # GitHub tools (if connected)
    if integrations.get('github'):
        parts.extend([
            "",
            "GITHUB INVESTIGATION:",
            "Use github_rca tool for structured code change investigation.",
            "",
            "IMPORTANT - Repository Auto-Resolution:",
            "- The tool auto-resolves repo from Knowledge Base (runbooks) if available",
            "- Do NOT pass repo= parameter unless you need a specific repo",
            "- Resolution order: KB Memory → KB Documents → Connected repo (fallback)",
            "",
            "Commands (omit repo= to use KB auto-resolution):",
            "- github_rca(action='deployment_check') - Recent GitHub Actions runs",
            "- github_rca(action='commits', incident_time='ALERT_TIME') - Recent commits",
            "- github_rca(action='diff', commit_sha='SHA') - Diff for specific commits",
            "- github_rca(action='pull_requests') - Recently merged PRs",
            "",
            "Check for recent code changes that may correlate with the alert.",
            "Look for: config changes, k8s manifests, Terraform, dependency updates.",
        ])

    # Confluence search tools (if connected)
    if integrations.get('confluence'):
        parts.extend([
            "",
            "CONFLUENCE INVESTIGATION:",
            "Use Confluence search tools to find prior incidents and runbooks:",
            "- confluence_search_similar(keywords=['error msg'], service_name='svc') - Find postmortems / past incidents",
            "- confluence_search_runbooks(service_name='svc') - Find runbooks / SOPs / playbooks",
            "- confluence_fetch_page(page_id='12345') - Read full page content as markdown",
            "",
            "Workflow: search first, then fetch promising pages for detailed procedures.",
            "Cross-reference Confluence findings with live infrastructure state.",
        ])

    # Coroot observability (if connected)
    if integrations.get('coroot'):
        parts.extend([
            "",
            "COROOT OBSERVABILITY (CONNECTED - USE THESE TOOLS):",
            "Coroot is an eBPF-powered observability platform. Its node agent instruments at the KERNEL level,",
            "capturing data that applications cannot self-report and requires NO code changes or SDK integration.",
            "",
            "WHAT eBPF GIVES YOU (data invisible to application logs):",
            "- TCP connections: every connect/accept/close between services, including failed connects and retransmissions",
            "- Network latency: actual round-trip time measured at the kernel, not application-reported",
            "- DNS queries: every resolution with latency, NXDOMAIN errors, and server failures",
            "- Disk I/O: per-process read/write latency and throughput at the block device level",
            "- Container resources: CPU usage, memory RSS, OOM kills, throttling — from cgroups",
            "- L7 protocol parsing: HTTP, PostgreSQL, MySQL, Redis, MongoDB, Memcached request/response metrics",
            "  extracted from TCP streams without application instrumentation",
            "- Service map: automatically discovered from observed TCP connections — not configured manually",
            "",
            "This means Coroot sees issues BEFORE they appear in application logs:",
            "- A service failing to connect to a dependency (TCP connect failures)",
            "- Network packet loss and retransmissions between pods/nodes",
            "- DNS resolution failures causing timeouts",
            "- Disk I/O saturation causing slow queries",
            "- OOM kills that happen before the app can log anything",
            "- Container CPU throttling invisible to the application",
            "",
            "INVESTIGATION FLOW:",
            "1. coroot_get_incidents(lookback_hours=24) — List incidents with RCA summaries, root cause, and fixes",
            "2. coroot_get_overview_logs(severity='Error', limit=50) — Search all logs cluster-wide for errors",
            "   (includes Kubernetes Events: OOMKilled, Evicted, CrashLoopBackOff, FailedScheduling)",
            "3. coroot_get_incident_detail(incident_key='KEY') — Full incident detail with propagation map",
            "4. coroot_get_app_detail(app_id='ID') — Audit reports for affected app (35+ health checks)",
            "5. coroot_get_app_logs(app_id='ID', severity='Error') — Error logs with trace correlation",
            "6. coroot_get_traces(service_name='svc', status_error=True) — Error traces across services",
            "7. coroot_get_traces(trace_id='ID') — Full trace tree for a specific request",
            "",
            "PROACTIVE HEALTH SCAN:",
            "1. coroot_get_applications() — All apps sorted by status (CRITICAL first)",
            "2. coroot_get_service_map() — Auto-discovered dependencies from eBPF TCP tracking",
            "3. coroot_get_deployments(lookback_hours=24) — Correlate deploys with failures",
            "4. coroot_get_risks() — Security and availability risks (single-instance, single-AZ, exposed ports)",
            "",
            "NODE INVESTIGATION:",
            "1. coroot_get_nodes() — List all nodes with health status",
            "2. coroot_get_node_detail(node_name='NODE') — Full audit (CPU, memory, disk, network per-interface)",
            "",
            "COST INVESTIGATION:",
            "1. coroot_get_costs(lookback_hours=24) — Cost breakdown per node/app + right-sizing recommendations",
            "   (cost spikes correlate with autoscaling issues, memory leaks, retry storms)",
            "",
            "METRICS (PromQL via Coroot — all collected by eBPF, no exporters needed):",
            "coroot_query_metrics(promql='rate(container_resources_cpu_usage_seconds_total[5m])')",
            "Key queries: CPU, memory RSS, OOM kills, HTTP error rate, TCP connect failures,",
            "  TCP retransmissions, network RTT, DNS latency, DB query latency, container restarts",
            "",
            "Status codes: 0=UNKNOWN, 1=OK, 2=INFO, 3=WARNING, 4=CRITICAL",
            "Check Coroot FIRST for any infrastructure-layer issue — it sees kernel-level events that",
            "application logs and cloud provider metrics cannot capture.",
        ])

    # Jenkins CI/CD (if connected)
    if integrations.get('jenkins'):
        parts.extend([
            "",
            "JENKINS CI/CD INVESTIGATION:",
            "Jenkins is connected. Use the `jenkins_rca` tool for CI/CD investigation.",
            "Actions: recent_deployments, build_detail, pipeline_stages, stage_log,",
            "  build_logs, test_results, blue_ocean_run, blue_ocean_steps, trace_context",
            "",
            "INVESTIGATION FLOW:",
            "1. jenkins_rca(action='recent_deployments', service='SERVICE') — Check for recent deploys",
            "2. jenkins_rca(action='build_detail', job_path='JOB', build_number=N) — Build details + commits",
            "3. jenkins_rca(action='pipeline_stages', job_path='JOB', build_number=N) — Stage breakdown",
            "4. jenkins_rca(action='build_logs', job_path='JOB', build_number=N) — Console output",
            "5. jenkins_rca(action='test_results', job_path='JOB', build_number=N) — Test failures",
            "6. jenkins_rca(action='trace_context', deployment_event_id=ID) — OTel trace correlation",
            "",
            "Recent deployments are a leading indicator of root cause.",
            "Always check if a deployment occurred shortly before the alert fired.",
        ])

    # CloudBees CI (if connected)
    if integrations.get('cloudbees'):
        parts.extend([
            "",
            "CLOUDBEES CI/CD INVESTIGATION:",
            "CloudBees CI is connected. Use the `cloudbees_rca` tool for CI/CD investigation.",
            "Actions: recent_deployments, build_detail, pipeline_stages, stage_log,",
            "  build_logs, test_results, blue_ocean_run, blue_ocean_steps, trace_context",
            "",
            "INVESTIGATION FLOW:",
            "1. cloudbees_rca(action='recent_deployments', service='SERVICE') — Check for recent deploys",
            "2. cloudbees_rca(action='build_detail', job_path='JOB', build_number=N) — Build details + commits",
            "3. cloudbees_rca(action='pipeline_stages', job_path='JOB', build_number=N) — Stage breakdown",
            "4. cloudbees_rca(action='build_logs', job_path='JOB', build_number=N) — Console output",
            "5. cloudbees_rca(action='test_results', job_path='JOB', build_number=N) — Test failures",
            "6. cloudbees_rca(action='trace_context', deployment_event_id=ID) — OTel trace correlation",
            "",
            "Recent deployments are a leading indicator of root cause.",
            "Always check if a deployment occurred shortly before the alert fired.",
        ])

    # Knowledge Base search (always available for authenticated users)
    parts.extend([
        "",
        "KNOWLEDGE BASE:",
        "Use knowledge_base_search tool to find runbooks and documentation:",
        "- knowledge_base_search(query='service name error') - Search for relevant docs",
        "- Check KB FIRST for troubleshooting procedures before cloud investigation",
    ])

    # OpenSSH fallback
    parts.extend([
        "",
        "VM ACCESS - Automatic SSH Keys Available, use OpenSSH terminal_exec to SSH into VMs:",
        "For OVH/Scaleway VMs configured via Aurora UI: Keys auto-mounted at ~/.ssh/id_<provider>_<vm_id>",
        "SSH command: terminal_exec('ssh -i ~/.ssh/id_scaleway_<VM_ID> -o StrictHostKeyChecking=no -o BatchMode=yes root@IP \"command\"')",
        "Or simpler: terminal_exec('ssh root@IP \"command\"') - keys in ~/.ssh/ tried automatically",
        "Users: GCP=admin | AWS=ec2-user/ubuntu | Azure=azureuser | OVH=debian/ubuntu/root | Scaleway=root",
    ])

    # CONTEXT UPDATE AWARENESS - CRITICAL
    parts.extend([
        "",
        "CONTEXT UPDATE AWARENESS - CRITICAL:",
        "During RCA investigations, you may receive CORRELATED INCIDENT CONTEXT UPDATEs via SystemMessage.",
        "These updates contain NEW incident data arriving mid-investigation (PagerDuty, monitoring, etc.).",
        "",
        "When you receive a context update message:",
        "1. IMMEDIATELY pivot your investigation to incorporate the new information",
        "2. STEER your next tool calls based on the update content",
        "3. Correlate new data with previous findings to identify patterns",
        "4. Adjust your investigation path - the update may reveal the root cause or new symptoms",
        "",
        "Examples:",
        "- Update shows new error in different service → investigate that service immediately",
        "- Update contains timeline data → correlate with your previous findings",
        "- Update identifies affected resources → focus investigation on those resources",
        "",
        "Context updates are HIGH PRIORITY - they represent LIVE incident evolution.",
        "=" * 40,
        "",
    ])

    # Critical requirements - MUST complete all before stopping
    if source == 'slack':
        # Slack-specific instructions (Concise, no heavy mandatory steps)
        parts.extend([
            "",
            "SLACK CONVERSATION CONTEXT:",
            "The user's message includes 'Recent conversation context' section with previous Slack thread messages.",
            "ALWAYS review this context - users reference earlier messages with 'earlier', 'that', 'it', etc.",
            "Build on the conversation - don't ignore what was already discussed in the thread.",
            "",
            "SLACK FORMATTING REQUIREMENTS:",
            "Use Slack markdown: *bold*, _italic_, `code`, ```code blocks```",
            "Structure responses: *Section Headers* + bullet points (•) or numbered lists",
            "Keep paragraphs short (2-3 sentences max)",
            "NO HTML, NO dropdowns, NO complex UI - plain text only",
            "",
            "INVESTIGATION GUIDANCE:",
            "Use tools when needed: kubectl, cloud commands, logs, metrics",
            "Check actual state with tool calls - don't assume",
            "For troubleshooting: Investigate thoroughly (resources → logs → root cause → fix)",
            "For info requests: Answer directly if you have the data",
            "Include specific evidence: exact errors, metrics, timestamps, resource names",
            "",
        ])
        # Reuse provider commands, fallbacks, and SSH troubleshooting from shared functions
        parts.extend([
            "RESPONSE FORMAT:",
            "1. *Direct answer* - respond to the question immediately (1-2 sentences)",
            "2. *Evidence* - show findings from investigation or supporting data",
            "3. *Next steps* - actionable recommendations if needed (numbered list)",
            "",
            "READ-ONLY mode - investigate only, no changes unless explicitly requested.",
            "=" * 40,
        ])
    else:
        parts.extend([
            "",
            "MANDATORY INVESTIGATION STEPS - DO NOT STOP UNTIL ALL ARE DONE:",
            f"1. List resources from EVERY provider: {', '.join(providers) if providers else 'None'}",
            "2. SSH into at least one affected VM (use OpenSSH terminal_exec above)",
            "3. Check system metrics: top, free -m, df -h, dmesg | tail",
            "4. Check logs: journalctl, /var/log/, cloud logging",
            "5. Identify root cause with evidence",
            "6. Provide remediation steps",
            "",
            "YOU MUST make 15-20+ tool calls. After EACH tool call, continue investigating.",
            "NEVER stop after listing resources - that's just step 1.",
            "On failure: try 3-4 alternatives immediately.",
            "",
            "READ-ONLY mode - investigate only, no changes.",
            "=" * 40,
        ])

    return "\n".join(parts)


def build_knowledge_base_memory_segment(user_id: Optional[str]) -> str:
    """Build knowledge base memory segment for system prompt.

    Fetches user's knowledge base memory content and formats it for injection
    into the system prompt. This content is always included for authenticated users.
    """
    if not user_id:
        return ""

    import logging
    kb_logger = logging.getLogger(__name__)

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            cursor.execute(
                "SELECT content FROM knowledge_base_memory WHERE user_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()

        if row and row[0] and row[0].strip():
            content = row[0].strip()
            # Escape curly braces for LangChain template compatibility
            content = content.replace("{", "{{").replace("}", "}}")

            return (
                "=" * 40 + "\n"
                "USER-PROVIDED CONTEXT (Knowledge Base Memory)\n"
                "=" * 40 + "\n"
                "The user has provided the following context that should inform your analysis:\n\n"
                f"{content}\n\n"
                "Consider this context when investigating issues and making recommendations.\n"
                "=" * 40 + "\n"
            )
    except Exception as e:
        kb_logger.warning(f"[KB] Error fetching knowledge base memory for user {user_id}: {e}")

    return ""


def build_prompt_segments(provider_preference: Optional[Any], mode: Optional[str], has_zip_reference: bool, state: Optional[Any] = None) -> PromptSegments:
    _, _, provider_constraints = build_provider_constraints(provider_preference)

    # Build system invariant
    system_invariant = build_system_invariant()
    
    provider_context = build_provider_context_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
        mode=mode,
    )

    prerequisite_checks = build_prerequisite_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
    )

    terraform_validation = build_terraform_validation_segment(state)

    model_overlay = build_model_overlay_segment(
        getattr(state, 'model', None) if state else None,
        provider_preference=provider_preference,
    )

    failure_recovery = build_failure_recovery_segment(state)
    manual_vm_access = build_manual_vm_access_segment(getattr(state, "user_id", None))

    # Build background mode segment if applicable (for RCA background chats)
    background_mode = build_background_mode_segment(state)

    # Build GitHub context for authenticated users with GitHub connected
    github_context = ""
    if state and hasattr(state, 'user_id'):
        github_context = build_github_context_segment(state.user_id)

    # Build Bitbucket context for authenticated users with Bitbucket connected
    bitbucket_context = ""
    if state and hasattr(state, 'user_id'):
        bitbucket_context = build_bitbucket_context_segment(state.user_id)

    # Build kubectl on-prem context for all users
    kubectl_onprem = ""
    if state and hasattr(state, 'user_id'):
        kubectl_onprem = build_kubectl_onprem_segment(state.user_id)

    # Build knowledge base memory context for authenticated users
    knowledge_base_memory = ""
    if state and hasattr(state, 'user_id'):
        knowledge_base_memory = build_knowledge_base_memory_segment(state.user_id)

    return PromptSegments(
        system_invariant=system_invariant,
        provider_constraints=provider_constraints,
        regional_rules=build_regional_rules(),
        ephemeral_rules=build_ephemeral_rules(mode),
        long_documents_note=build_long_documents_note(has_zip_reference),
        provider_context=provider_context,
        prerequisite_checks=prerequisite_checks,
        terraform_validation=terraform_validation,
        model_overlay=model_overlay,
        failure_recovery=failure_recovery,
        background_mode=background_mode,
        github_context=github_context,
        bitbucket_context=bitbucket_context,
        manual_vm_access=manual_vm_access,
        kubectl_onprem=kubectl_onprem,
        knowledge_base_memory=knowledge_base_memory,
    )


def assemble_system_prompt(segments: PromptSegments) -> str: #main prompt builder
    parts: List[str] = []
    # Background mode comes first if present (important RCA context)
    if segments.background_mode:
        parts.append(segments.background_mode)
    # Knowledge base memory comes early (user-provided context for all investigations)
    if segments.knowledge_base_memory:
        parts.append(segments.knowledge_base_memory)
    if segments.ephemeral_rules:
        parts.append(segments.ephemeral_rules)
    if segments.model_overlay:
        parts.append(segments.model_overlay)
    if segments.provider_context:
        parts.append(segments.provider_context)
    if segments.manual_vm_access:
        parts.append(segments.manual_vm_access)
    if segments.github_context:
        parts.append(segments.github_context)
    if segments.bitbucket_context:
        parts.append(segments.bitbucket_context)
    if segments.kubectl_onprem:
        parts.append(segments.kubectl_onprem)
    if segments.prerequisite_checks:
        parts.append(segments.prerequisite_checks)
    parts.append(segments.system_invariant)
    parts.append(segments.provider_constraints)
    parts.append(segments.regional_rules)
    if segments.long_documents_note:
        parts.append(segments.long_documents_note)
    if segments.terraform_validation:
        parts.append(segments.terraform_validation)
    if segments.failure_recovery:
        parts.append(segments.failure_recovery)
    return "\n".join(parts)


def register_prompt_cache_breakpoints(
    pcm: PrefixCacheManager,
    segments: PromptSegments,
    tools: List[Any],
    provider: str,
    tenant_id: str,
) -> None:
    # Cache stable segments with regular TTL
    pcm.register_segment(
        segment_name="system_invariant",
        content=segments.system_invariant,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="provider_constraints",
        content=segments.provider_constraints,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="regional_rules",
        content=segments.regional_rules,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    if segments.provider_context:
        pcm.register_segment(
            segment_name="provider_context",
            content=segments.provider_context,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.github_context:
        pcm.register_segment(
            segment_name="github_context",
            content=segments.github_context,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.bitbucket_context:
        pcm.register_segment(
            segment_name="bitbucket_context",
            content=segments.bitbucket_context,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.prerequisite_checks:
        pcm.register_segment(
            segment_name="prerequisite_checks",
            content=segments.prerequisite_checks,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.terraform_validation:
        pcm.register_segment(
            segment_name="terraform_validation",
            content=segments.terraform_validation,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.model_overlay:
        pcm.register_segment(
            segment_name="model_overlay",
            content=segments.model_overlay,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.failure_recovery:
        pcm.register_segment(
            segment_name="failure_recovery",
            content=segments.failure_recovery,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    # Tie tool schema/version into a dedicated segment so cache invalidates when tool defs change
    pcm.register_segment(
        segment_name="tools_manifest",
        content="Tool definitions and parameter shapes",
        provider=provider,
        tenant_id=tenant_id,
        tools=tools,
        ttl_s=None,
    )
    # Ephemeral rules are not cached (or can be set to very short TTL if desired)
    if segments.ephemeral_rules:
        pcm.register_segment(
            segment_name="ephemeral_rules",
            content=segments.ephemeral_rules,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        ) 
