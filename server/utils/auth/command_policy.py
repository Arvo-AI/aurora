"""Org-level command policy engine (allowlist / denylist).

Evaluates every command before execution against org-configured regex rules.
Two independent lists, each independently togglable:

  1. Denylist (if enabled) - checked first. Match -> DENIED.
  2. Allowlist (if enabled) - checked second. Match -> ALLOWED, no match -> DENIED.
  3. Both off -> ALLOWED.

Default for new orgs: allowlist ON, denylist OFF.
Fail-closed: any error fetching policy -> deny all.
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
    """Load policy rules and list states for *org_id*.

    Fail-closed: on ANY error, returns empty rules with allowlist ON.
    With zero allow rules and allowlist enabled, evaluate_command denies everything.
    """
    allow_rules: List[PolicyRule] = []
    deny_rules: List[PolicyRule] = []
    states = ListStates(allowlist_enabled=True, denylist_enabled=False)

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
        al_raw = get_user_preference(org_pref_key, "command_policy_allowlist") or "on"
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
        return CommandVerdict(allowed=True)

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
        "The following command policy is enforced. Commands violating this policy "
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
    """Default seed templates, inserted on first enable of each list."""
    return {
        "allow": [
            {"priority": 100, "pattern": r"^(ls|cat|head|tail|wc|grep|find|stat|file|du|df)\b",
             "description": "Read-only filesystem inspection"},
            {"priority": 90, "pattern": r"^(kubectl|oc)\s+(get|describe|logs|top|explain)\b",
             "description": "Read-only Kubernetes queries"},
            {"priority": 80, "pattern": r"^(gcloud|aws|az)\s+.*\b(list|describe|get|show)\b",
             "description": "Read-only cloud CLI queries"},
            {"priority": 70, "pattern": r"^(terraform|tofu)\s+(init|plan|validate|fmt|output|state\s+(list|show|pull))\b",
             "description": "Non-destructive Terraform operations"},
            {"priority": 60, "pattern": r"^(ping|dig|nslookup|traceroute|curl|wget)\b",
             "description": "Network diagnostics"},
            {"priority": 50, "pattern": r"^git\s+(status|log|diff|show|branch)\b",
             "description": "Read-only git operations"},
        ],
        "deny": [
            {"priority": 100, "pattern": r"\brm\s+-rf\s+/",
             "description": "Recursive root deletion"},
            {"priority": 95, "pattern": r"\b(gcc|g\+\+|cc|make|as|ld)\b",
             "description": "Native code compilation"},
            {"priority": 90, "pattern": r"\bLD_PRELOAD\b",
             "description": "Shared library injection"},
            {"priority": 85, "pattern": r"\b(ssh-keygen|ssh-copy-id)\b",
             "description": "SSH key generation on host"},
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
            {"priority": 50, "pattern": r"\bkubectl\s+(exec|cp|run|attach|port-forward)\b",
             "description": "Interactive kubectl operations"},
        ],
    }


def invalidate_cache(org_id: str) -> None:
    _cache.pop(org_id, None)
