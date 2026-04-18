"""Org-level command policy engine (allowlist / denylist).

Evaluates every command before execution against org-configured regex rules.
Two independent lists, each independently togglable:

  1. Denylist (if enabled) - checked first. Match -> DENIED.
  2. Allowlist (if enabled) - checked second. Match -> ALLOWED, no match -> DENIED.
  3. Both off -> ALLOWED.

Compound shell expressions (;, &&, ||, |, subshells) are decomposed and each
atomic command is evaluated independently. One denied sub-command blocks the
entire expression.

Default for new orgs: both lists OFF (no enforcement until configured).
Fail-open on DB error: if rules cannot be fetched, commands are allowed.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CACHE_TTL = 30  # seconds

_CacheEntry = Tuple[
    List["PolicyRule"],  # allow rules
    List["PolicyRule"],  # deny rules
    "ListStates",
    float,               # monotonic timestamp
]
_cache: Dict[str, _CacheEntry] = {}


@dataclass(frozen=True)
class CommandVerdict:
    allowed: bool
    rule_description: Optional[str] = None


@dataclass(frozen=True)
class PolicyRule:
    id: int
    mode: str
    pattern: str
    description: str
    priority: int
    compiled: re.Pattern = field(repr=False)


@dataclass(frozen=True)
class ListStates:
    allowlist_enabled: bool
    denylist_enabled: bool


def _compile_safe(pattern: str) -> Optional[re.Pattern]:
    try:
        return re.compile(pattern)
    except re.error:
        logger.warning("Invalid regex in policy rule, skipping: %s", pattern)
        return None


def _fetch(org_id: str) -> Tuple[List[PolicyRule], List[PolicyRule], ListStates]:
    """Load policy rules and list states for *org_id*."""
    allow_rules: List[PolicyRule] = []
    deny_rules: List[PolicyRule] = []
    states = ListStates(allowlist_enabled=False, denylist_enabled=False)

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import get_user_preference

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, mode, pattern, description, priority "
                    "FROM org_command_policies "
                    "WHERE org_id = %s AND enabled = true "
                    "ORDER BY priority DESC",
                    (org_id,),
                )
                for row in cur.fetchall():
                    compiled = _compile_safe(row[2])
                    if compiled is None:
                        continue
                    rule = PolicyRule(
                        id=row[0], mode=row[1], pattern=row[2],
                        description=row[3] or "", priority=row[4],
                        compiled=compiled,
                    )
                    if rule.mode == "allow":
                        allow_rules.append(rule)
                    else:
                        deny_rules.append(rule)

        org_pref_key = f"__org__{org_id}"
        al_raw = get_user_preference(org_pref_key, "command_policy_allowlist") or "off"
        dl_raw = get_user_preference(org_pref_key, "command_policy_denylist") or "off"
        states = ListStates(
            allowlist_enabled=(str(al_raw).lower() == "on"),
            denylist_enabled=(str(dl_raw).lower() == "on"),
        )
    except Exception:
        logger.exception("Failed to fetch command policies for org %s, fail-closed", org_id)

    return allow_rules, deny_rules, states


def _get_cached(org_id: str) -> Tuple[List[PolicyRule], List[PolicyRule], ListStates]:
    entry = _cache.get(org_id)
    if entry is not None:
        allow, deny, states, ts = entry
        if time.monotonic() - ts < _CACHE_TTL:
            return allow, deny, states

    allow, deny, states = _fetch(org_id)
    _cache[org_id] = (allow, deny, states, time.monotonic())
    return allow, deny, states


def evaluate_command(org_id: Optional[str], command: str) -> CommandVerdict:
    """Core gate. Returns whether *command* is allowed for *org_id*."""
    if not org_id:
        return CommandVerdict(allowed=True)

    allow_rules, deny_rules, states = _get_cached(org_id)

    if not states.denylist_enabled and not states.allowlist_enabled:
        return CommandVerdict(allowed=True, rule_description="Policy lists are disabled")

    if states.denylist_enabled:
        for rule in deny_rules:
            if rule.compiled.search(command):
                return CommandVerdict(allowed=False, rule_description=rule.description)

    if states.allowlist_enabled:
        for rule in allow_rules:
            if rule.compiled.search(command):
                return CommandVerdict(allowed=True, rule_description=rule.description)
        return CommandVerdict(allowed=False, rule_description="No matching allow rule")

    return CommandVerdict(allowed=True)


def _split_compound_command(compound: str) -> List[str]:
    """Quote-aware split of a shell expression into atomic commands.

    Splits on ; && || | while respecting single/double quotes and backslash
    escapes.  Recursively extracts commands from $(...) and backtick subshells
    so they are evaluated independently.
    """
    commands: List[str] = []
    buf: List[str] = []
    sq = dq = False
    i, n = 0, len(compound)

    def _flush() -> None:
        s = "".join(buf).strip()
        if s:
            commands.append(s)
        buf.clear()

    while i < n:
        c = compound[i]

        # Backslash escape (not inside single quotes)
        if c == "\\" and not sq and i + 1 < n:
            buf += [c, compound[i + 1]]
            i += 2
            continue

        # Quote toggling
        if c == "'" and not dq:
            sq = not sq
            buf.append(c)
            i += 1
            continue
        if c == '"' and not sq:
            dq = not dq
            buf.append(c)
            i += 1
            continue

        # Everything inside quotes is literal
        if sq or dq:
            buf.append(c)
            i += 1
            continue

        # -- Outside quotes: detect operators and subshells --
        two = compound[i : i + 2]

        if two in ("&&", "||"):
            _flush()
            i += 2
            continue

        if c in (";", "|"):
            _flush()
            i += 1
            continue

        # $(...) subshell - extract inner commands for separate evaluation
        if c == "$" and i + 1 < n and compound[i + 1] == "(":
            j, depth = i + 2, 1
            while j < n and depth:
                if compound[j] == "(":
                    depth += 1
                elif compound[j] == ")":
                    depth -= 1
                j += 1
            commands.extend(_split_compound_command(compound[i + 2 : j - 1]))
            buf.append(compound[i:j])
            i = j
            continue

        # Backtick subshell
        if c == "`":
            j = compound.find("`", i + 1)
            if j != -1:
                commands.extend(_split_compound_command(compound[i + 1 : j]))
                buf.append(compound[i : j + 1])
                i = j + 1
                continue

        buf.append(c)
        i += 1

    _flush()
    return commands


def evaluate_compound_command(
    org_id: Optional[str], command: str
) -> CommandVerdict:
    """Evaluate a potentially compound shell command.

    Decomposes the expression into atomic commands and evaluates each one
    independently.  ALL sub-commands must pass policy; the first denial is
    returned immediately.
    """
    if not org_id:
        return CommandVerdict(allowed=True)

    parts = _split_compound_command(command)
    if not parts:
        return evaluate_command(org_id, command)

    last_verdict = CommandVerdict(allowed=True)
    for part in parts:
        verdict = evaluate_command(org_id, part)
        if not verdict.allowed:
            return verdict
        last_verdict = verdict

    return last_verdict


def validate_pattern(pattern: str) -> Optional[str]:
    """Return an error string if *pattern* is not valid regex, else None."""
    try:
        re.compile(pattern)
        return None
    except re.error as exc:
        return str(exc)


def get_policy_prompt_text(org_id: str) -> str:
    """Render active policy as system-prompt text for the LLM."""
    allow_rules, deny_rules, states = _get_cached(org_id)

    if not states.denylist_enabled and not states.allowlist_enabled:
        return ""

    lines = [
        "## Organization Command Policy",
        "The following command policy is enforced. Commands violating this policy " +
        "will be rejected at execution time. Do not attempt blocked commands.",
        "",
    ]

    if states.denylist_enabled and deny_rules:
        lines.append("DENIED commands (never run these):")
        for r in deny_rules:
            lines.append(f"  - {r.description} (pattern: {r.pattern})")
        lines.append("")

    if states.allowlist_enabled and allow_rules:
        lines.append("ALLOWED commands (only these are permitted):")
        for r in allow_rules:
            lines.append(f"  - {r.description} (pattern: {r.pattern})")
        lines.append("")

    return "\n".join(lines)


def get_seed_rules() -> Dict[str, list]:
    """Default seed templates, inserted on first enable of each list.

    Returns the 'observability_only' template for backward compatibility.
    """
    tpl = get_policy_templates()[0]
    return {"allow": tpl["allow"], "deny": tpl["deny"]}


# ---------------------------------------------------------------------------
# Deny rules shared across ALL templates (dangerous regardless of access level)
# ---------------------------------------------------------------------------
_UNIVERSAL_DENY_RULES: list = [
    {"priority": 100, "pattern": r"\brm\s+-rf\s+/",
     "description": "Recursive root deletion"},
    {"priority": 95, "pattern": r"\b(gcc|g\+\+|cc|make|as|ld)\b",
     "description": "Native code compilation"},
    {"priority": 92, "pattern": r"(?<!\bkubectl\s)(?<!\boc\s)(?<!\bdocker\s)\b(eval|exec)\b",
     "description": "Dynamic code evaluation"},
    {"priority": 90, "pattern": r"\bLD_PRELOAD\b",
     "description": "Shared library injection"},
    {"priority": 88, "pattern": r"\bbase64\b.*\|\s*(sh|bash|python)",
     "description": "Encoded payload execution"},
    {"priority": 85, "pattern": r"\b(ssh-keygen|ssh-copy-id)\b",
     "description": "SSH key generation on host"},
    {"priority": 83, "pattern": r"\b(bash|sh|dash|zsh)\s+-c\b",
     "description": "Inline shell interpreter"},
    {"priority": 80, "pattern": r"\b(useradd|usermod|adduser|visudo|passwd)\b",
     "description": "User/privilege management"},
    {"priority": 75, "pattern": r"\bcurl\b.*\|\s*(sh|bash)\b",
     "description": "Remote script execution"},
    {"priority": 70, "pattern": r"\b(nc|ncat|netcat|socat)\b.*(-l|-e|-c)\b",
     "description": "Network listener / reverse shell"},
    {"priority": 65, "pattern": r"\bchmod\b.*(\+s|u\+s|4[0-7]{3})\b",
     "description": "SUID bit manipulation"},
    {"priority": 60, "pattern": r"\bnsenter\b|\bunshare\b|\bchroot\b",
     "description": "Namespace/container escape"},
    {"priority": 55, "pattern": r"\biptables\b|\bnft\b|\bip\s+route\b",
     "description": "Network configuration changes"},
]


def get_policy_templates() -> List[dict]:
    """Return the library of pre-built policy templates.

    Each template is a dict with keys: id, name, description, allow, deny.
    Templates are ordered from most restrictive to most permissive.
    """
    return [
        # -- 1. Observability Only ----------------------------------------
        {
            "id": "observability_only",
            "name": "Observability Only",
            "description": (
                "Read-only access aligned with cloud provider read-only "
                "credentials (AWS ReadOnlyAccess session policy, GCP "
                "roles/viewer, Azure Reader). Blocks all write, SSH, and "
                "interactive operations."
            ),
            "allow": [
                # Filesystem inspection
                {"priority": 200, "pattern": r"^(ls|cat|head|tail|wc|grep|find|stat|file|du|df|sort|uniq|awk|sed|tr|cut|tee|less|more|xargs|realpath|readlink|basename|dirname)\b",
                 "description": "Read-only filesystem inspection"},
                # Kubernetes read-only
                {"priority": 190, "pattern": r"^(kubectl|oc)\s+(get|describe|logs|top|explain|api-resources|api-versions|cluster-info|config\s+(view|get-contexts|current-context|use-context))\b",
                 "description": "Read-only Kubernetes queries"},
                # AWS CLI -- matches all read-only verbs and subcommands that the session policy permits
                {"priority": 180, "pattern": r"^aws\s+.+\b(ls|list|describe|get|show|head|filter|start-query|stop-query|test-metric-filter|update-kubeconfig|wait)\b",
                 "description": "AWS read-only operations (EC2, EKS, S3, RDS, Lambda, IAM, CloudWatch, Logs, ECS, CloudFormation)"},
                {"priority": 179, "pattern": r"^aws\s+s3\s+(ls|cp\s+s3://|presign|sync\s+s3://)",
                 "description": "AWS S3 read operations (ls, download, presign)"},
                {"priority": 178, "pattern": r"^aws\s+sts\s+get-caller-identity\b",
                 "description": "AWS STS identity check"},
                {"priority": 177, "pattern": r"^aws\s+logs\s+(describe-log-groups|describe-log-streams|get-log-events|get-query-results|filter-log-events|start-query|stop-query|tail)\b",
                 "description": "AWS CloudWatch Logs read operations"},
                # GCP / gcloud -- roles/viewer, logging.viewer, monitoring.viewer, container.viewer, storage.objectViewer
                {"priority": 170, "pattern": r"^gcloud\s+.+\b(list|describe|get|show|read|get-credentials|get-server-config)\b",
                 "description": "GCP read-only operations (Compute, GKE, Cloud SQL, Cloud Run, IAM, DNS)"},
                {"priority": 169, "pattern": r"^gcloud\s+(logging|monitoring|asset|projects|organizations|config)\s",
                 "description": "GCP logging, monitoring, asset inventory, and config"},
                {"priority": 168, "pattern": r"^gsutil\s+(ls|cat|stat|du|cp\s+gs://|rsync\s+-n)\b",
                 "description": "GCP Storage read operations (ls, cat, stat, download)"},
                {"priority": 167, "pattern": r"^bq\s+(ls|show|head|query\s+--dry_run|mk\s+--dry_run)\b",
                 "description": "BigQuery read-only operations"},
                # Azure -- Reader role + Log Analytics Reader + Monitoring Reader
                {"priority": 160, "pattern": r"^az\s+.+\b(list|show|get|describe|display|query|download)\b",
                 "description": "Azure read-only operations (VMs, AKS, Storage, SQL, Key Vault, NSGs)"},
                {"priority": 159, "pattern": r"^az\s+(monitor|advisor|security|consumption|costmanagement|account)\s",
                 "description": "Azure monitoring, cost, security, and account queries"},
                {"priority": 158, "pattern": r"^az\s+aks\s+get-credentials\b",
                 "description": "Azure AKS kubeconfig retrieval"},
                # OVH / Scaleway
                {"priority": 150, "pattern": r"^ovhcloud\s+.+\b(list|show|get|describe)\b",
                 "description": "OVH read-only operations"},
                {"priority": 149, "pattern": r"^scw\s+.+\b(list|get|describe|inspect)\b",
                 "description": "Scaleway read-only operations"},
                # Tailscale
                {"priority": 140, "pattern": r"^tailscale\s+(status|device\s+(list|get)|dns|acl\s+(get|show)|routes|settings|auth-key\s+list)\b",
                 "description": "Tailscale read-only operations"},
                # Terraform / IaC read-only
                {"priority": 130, "pattern": r"^(terraform|tofu)\s+(init|plan|validate|fmt|output|show|state\s+(list|show|pull)|version)\b",
                 "description": "Non-destructive Terraform operations"},
                {"priority": 129, "pattern": r"^(helm)\s+(list|get|show|status|history|search|version)\b",
                 "description": "Helm read-only operations"},
                # Network diagnostics
                {"priority": 120, "pattern": r"^(ping|dig|nslookup|traceroute|tracepath|mtr|curl|wget|host|whois|nmap)\b",
                 "description": "Network diagnostics"},
                # Git read-only
                {"priority": 110, "pattern": r"^git\s+(status|log|diff|show|branch|tag|remote|stash\s+list|rev-parse|config\s+--get|ls-files|ls-remote|blame|shortlog)\b",
                 "description": "Read-only git operations"},
                # Docker/container inspection
                {"priority": 100, "pattern": r"^(docker|podman)\s+(ps|images|inspect|logs|stats|top|port|diff|history|version|info|network\s+(ls|inspect)|volume\s+(ls|inspect))\b",
                 "description": "Container inspection (read-only)"},
                # System diagnostics
                {"priority": 90, "pattern": r"^(uptime|whoami|hostname|uname|env|printenv|id|date|cal|free|vmstat|iostat|mpstat|sar|lsof|ss|netstat|ps|top|htop|lscpu|lsmem|lsblk|mount|dmesg|journalctl|systemctl\s+(status|is-active|is-enabled|list-units|list-timers))\b",
                 "description": "System diagnostics and status"},
                # Process / text utilities
                {"priority": 80, "pattern": r"^(jq|yq|column|printf|echo|test|true|false|which|type|command|whereis|file|xxd|hexdump|sha256sum|md5sum|base64)\b",
                 "description": "Text processing and utility commands"},
            ],
            "deny": [
                *_UNIVERSAL_DENY_RULES,
                {"priority": 50, "pattern": r"\bkubectl\s+(exec|cp|run|attach|port-forward|apply|delete|create|edit|patch|replace|scale|rollout|drain|cordon|uncordon|taint)\b",
                 "description": "Mutating kubectl operations"},
                {"priority": 48, "pattern": r"^(ssh|scp|sftp)\s",
                 "description": "SSH access (use Standard Operations template to enable)"},
            ],
        },

        # -- 2. Standard Operations ----------------------------------------
        {
            "id": "standard_ops",
            "name": "Standard Operations",
            "description": (
                "Allows SSH, kubectl exec, cloud CLI config commands, and "
                "container inspection on top of full read-only access. "
                "Suitable for incident response and debugging. Still blocks "
                "infrastructure mutations and dangerous patterns."
            ),
            "allow": [
                # Filesystem
                {"priority": 200, "pattern": r"^(ls|cat|head|tail|wc|grep|find|stat|file|du|df|sort|uniq|awk|sed|tr|cut|tee|less|more|xargs|realpath|readlink|basename|dirname)\b",
                 "description": "Filesystem inspection"},
                # Kubernetes read + interactive
                {"priority": 190, "pattern": r"^(kubectl|oc)\s+(get|describe|logs|top|explain|api-resources|api-versions|cluster-info|config\s+(view|get-contexts|current-context|use-context)|exec|cp|attach|port-forward|run\s+.*--rm\b.*--restart=Never)\b",
                 "description": "Kubernetes read and interactive debug operations"},
                # AWS full read + config
                {"priority": 180, "pattern": r"^aws\s+.+\b(ls|list|describe|get|show|head|filter|start-query|stop-query|test-metric-filter|update-kubeconfig|configure|wait)\b",
                 "description": "AWS read and config operations"},
                {"priority": 179, "pattern": r"^aws\s+s3\s+(ls|cp\s+s3://|presign|sync\s+s3://)",
                 "description": "AWS S3 read operations"},
                {"priority": 178, "pattern": r"^aws\s+sts\s+(get-caller-identity|assume-role|get-session-token)\b",
                 "description": "AWS STS operations"},
                {"priority": 177, "pattern": r"^aws\s+logs\s+(describe-log-groups|describe-log-streams|get-log-events|get-query-results|filter-log-events|start-query|stop-query|tail)\b",
                 "description": "AWS CloudWatch Logs operations"},
                # GCP full read + credentials
                {"priority": 170, "pattern": r"^gcloud\s+.+\b(list|describe|get|show|read|get-credentials|get-server-config)\b",
                 "description": "GCP read and credential operations"},
                {"priority": 169, "pattern": r"^gcloud\s+(logging|monitoring|asset|projects|organizations|config|auth)\s",
                 "description": "GCP logging, monitoring, asset inventory, auth, and config"},
                {"priority": 168, "pattern": r"^gsutil\s+(ls|cat|stat|du|cp\s+gs://|rsync\s+-n)\b",
                 "description": "GCP Storage read operations"},
                {"priority": 167, "pattern": r"^bq\s+(ls|show|head|query|mk\s+--dry_run)\b",
                 "description": "BigQuery operations"},
                # Azure full read + credentials
                {"priority": 160, "pattern": r"^az\s+.+\b(list|show|get|describe|display|query|download|browse)\b",
                 "description": "Azure read operations"},
                {"priority": 159, "pattern": r"^az\s+(monitor|advisor|security|consumption|costmanagement|account|aks\s+get-credentials)\s",
                 "description": "Azure monitoring, cost, security, and AKS credentials"},
                # OVH / Scaleway
                {"priority": 150, "pattern": r"^ovhcloud\s+.+\b(list|show|get|describe)\b",
                 "description": "OVH read-only operations"},
                {"priority": 149, "pattern": r"^scw\s+.+\b(list|get|describe|inspect)\b",
                 "description": "Scaleway read-only operations"},
                # Tailscale
                {"priority": 140, "pattern": r"^tailscale\s+(status|device\s+(list|get)|dns|acl\s+(get|show)|routes|settings|auth-key\s+list)\b",
                 "description": "Tailscale read-only operations"},
                # SSH access
                {"priority": 135, "pattern": r"^(ssh|scp|sftp)\s",
                 "description": "SSH, SCP, and SFTP access"},
                # Terraform / IaC read-only
                {"priority": 130, "pattern": r"^(terraform|tofu)\s+(init|plan|validate|fmt|output|show|state\s+(list|show|pull)|version)\b",
                 "description": "Non-destructive Terraform operations"},
                {"priority": 129, "pattern": r"^(helm)\s+(list|get|show|status|history|search|version|template)\b",
                 "description": "Helm read-only operations"},
                # Network diagnostics
                {"priority": 120, "pattern": r"^(ping|dig|nslookup|traceroute|tracepath|mtr|curl|wget|host|whois|nmap)\b",
                 "description": "Network diagnostics"},
                # Git read-only
                {"priority": 110, "pattern": r"^git\s+(status|log|diff|show|branch|tag|remote|stash\s+list|rev-parse|config\s+--get|ls-files|ls-remote|blame|shortlog)\b",
                 "description": "Read-only git operations"},
                # Docker/container inspection + exec
                {"priority": 100, "pattern": r"^(docker|podman)\s+(ps|images|inspect|logs|stats|top|port|diff|history|version|info|exec|network\s+(ls|inspect)|volume\s+(ls|inspect))\b",
                 "description": "Container inspection and exec"},
                # System diagnostics
                {"priority": 90, "pattern": r"^(uptime|whoami|hostname|uname|env|printenv|id|date|cal|free|vmstat|iostat|mpstat|sar|lsof|ss|netstat|ps|top|htop|lscpu|lsmem|lsblk|mount|dmesg|journalctl|systemctl\s+(status|is-active|is-enabled|list-units|list-timers))\b",
                 "description": "System diagnostics and status"},
                # Text utilities
                {"priority": 80, "pattern": r"^(jq|yq|column|printf|echo|test|true|false|which|type|command|whereis|file|xxd|hexdump|sha256sum|md5sum|base64)\b",
                 "description": "Text processing and utility commands"},
            ],
            "deny": [
                *_UNIVERSAL_DENY_RULES,
                {"priority": 50, "pattern": r"\bkubectl\s+(apply|delete|create|edit|patch|replace|scale|rollout|drain|cordon|uncordon|taint)\b",
                 "description": "Mutating kubectl operations (use Full Cloud Access to enable)"},
            ],
        },

        # -- 3. Full Cloud Access ------------------------------------------
        {
            "id": "full_cloud_access",
            "name": "Full Cloud Access",
            "description": (
                "Broad command access for orgs with admin-level cloud "
                "credentials. Allows cloud write operations, Terraform "
                "apply, kubectl mutations, SSH, and Docker management. "
                "Only blocks universally dangerous patterns."
            ),
            "allow": [
                # Filesystem -- broad
                {"priority": 200, "pattern": r"^(ls|cat|head|tail|wc|grep|find|stat|file|du|df|sort|uniq|awk|sed|tr|cut|tee|less|more|xargs|realpath|readlink|basename|dirname|mkdir|cp|mv|touch|ln|chmod|chown|rm)\b",
                 "description": "Filesystem operations"},
                # Kubernetes -- full
                {"priority": 190, "pattern": r"^(kubectl|oc)\s+\w",
                 "description": "All kubectl/oc operations"},
                # AWS -- broad
                {"priority": 180, "pattern": r"^aws\s+\w",
                 "description": "All AWS CLI operations"},
                # GCP -- broad
                {"priority": 170, "pattern": r"^(gcloud|gsutil|bq)\s+\w",
                 "description": "All GCP CLI operations"},
                # Azure -- broad
                {"priority": 160, "pattern": r"^az\s+\w",
                 "description": "All Azure CLI operations"},
                # OVH / Scaleway -- broad
                {"priority": 150, "pattern": r"^(ovhcloud|scw)\s+\w",
                 "description": "All OVH and Scaleway CLI operations"},
                # Tailscale
                {"priority": 140, "pattern": r"^tailscale\s+\w",
                 "description": "All Tailscale operations"},
                # SSH
                {"priority": 135, "pattern": r"^(ssh|scp|sftp)\s",
                 "description": "SSH, SCP, and SFTP access"},
                # Terraform -- full
                {"priority": 130, "pattern": r"^(terraform|tofu|pulumi)\s+\w",
                 "description": "All Terraform/Tofu/Pulumi operations"},
                {"priority": 129, "pattern": r"^(helm|helmfile)\s+\w",
                 "description": "All Helm operations"},
                {"priority": 128, "pattern": r"^(ansible|ansible-playbook|ansible-galaxy)\s",
                 "description": "All Ansible operations"},
                # Network diagnostics
                {"priority": 120, "pattern": r"^(ping|dig|nslookup|traceroute|tracepath|mtr|curl|wget|host|whois|nmap)\b",
                 "description": "Network diagnostics"},
                # Git -- full
                {"priority": 110, "pattern": r"^git\s+\w",
                 "description": "All git operations"},
                # Docker/container -- full
                {"priority": 100, "pattern": r"^(docker|podman|docker-compose|ctr|crictl)\s+\w",
                 "description": "All container operations"},
                # System diagnostics + management
                {"priority": 90, "pattern": r"^(uptime|whoami|hostname|uname|env|printenv|id|date|cal|free|vmstat|iostat|mpstat|sar|lsof|ss|netstat|ps|top|htop|lscpu|lsmem|lsblk|mount|dmesg|journalctl|systemctl)\b",
                 "description": "System diagnostics and service management"},
                # Text/utility
                {"priority": 80, "pattern": r"^(jq|yq|column|printf|echo|test|true|false|which|type|command|whereis|file|xxd|hexdump|sha256sum|md5sum|base64|tar|gzip|gunzip|zip|unzip|xz)\b",
                 "description": "Text processing and archive utilities"},
                # Package managers (read)
                {"priority": 70, "pattern": r"^(pip|npm|yarn|go|cargo|apt|yum|dnf|brew)\s+(list|show|info|search|outdated|version|--version)\b",
                 "description": "Package manager queries"},
            ],
            "deny": list(_UNIVERSAL_DENY_RULES),
        },
    ]


def invalidate_cache(org_id: str) -> None:
    _cache.pop(org_id, None)
