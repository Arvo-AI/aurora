"""Static signature matcher for known-malicious command patterns.

Compiled regex rules run against every command before the LLM judge.
Catches ~80% of dangerous commands in <5ms with zero cost.

Rule categories map to MITRE ATT&CK techniques where applicable.

Sigma rules from vendored SigmaHQ YAML files are appended after the
hand-written rules when ``config.sigma_enabled`` is True.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignatureVerdict:
    matched: bool
    technique: str = ""
    rule_id: str = ""
    description: str = ""


# MITRE ATT&CK technique identifiers used by the rule table below.
# Centralized to avoid duplicating the literal strings across rules.
T_UNIX_SHELL = "T1059.004"          # Command and Scripting Interpreter: Unix Shell
T_PYTHON = "T1059.006"              # Command and Scripting Interpreter: Python
T_SIGNED_BINARY = "T1218"           # Signed Binary Proxy Execution
T_HIJACK_EXEC_FLOW = "T1574.006"    # Dynamic Linker Hijacking
T_OS_CRED_LSASS = "T1003.001"       # OS Credential Dumping: LSASS
T_OS_CRED_SHADOW = "T1003.008"      # OS Credential Dumping: /etc/passwd and /etc/shadow
T_CRED_CLOUD = "T1552.001"          # Credentials In Files
T_CRED_SSH = "T1552.004"            # Private Keys
T_CRED_CONTAINER_API = "T1552.007"  # Container API credentials
T_SCHED_TASK_CRON = "T1053.003"     # Scheduled Task/Job: Cron
T_ACCOUNT_MANIP_SSH = "T1098.004"   # Account Manipulation: SSH Authorized Keys
T_IMPAIR_DEFENSES = "T1562.001"     # Impair Defenses: Disable or Modify Tools
T_IMPAIR_FIREWALL = "T1562.004"     # Impair Defenses: Disable or Modify System Firewall
T_CLEAR_HISTORY = "T1070.003"       # Indicator Removal: Clear Command History
T_RESOURCE_HIJACK = "T1496"         # Resource Hijacking
T_DATA_DESTRUCTION = "T1485"        # Data Destruction
T_DISK_WIPE = "T1561"               # Disk Wipe
T_ENDPOINT_DOS = "T1499.004"        # Endpoint DoS: Application or System Exploitation
T_ABUSE_ELEVATION = "T1548"         # Abuse Elevation Control Mechanism


# Each rule: (compiled_pattern, technique, rule_id, description)
_Rule = tuple[re.Pattern, str, str, str]

_RULES: list[_Rule] = []


def _r(pattern: str, technique: str, rule_id: str, desc: str) -> None:
    _RULES.append((re.compile(pattern, re.IGNORECASE), technique, rule_id, desc))


# --- LOLBins / Encoded payload execution (T1059) ---
_r(r"base64\s.*\|\s*(sh|bash|zsh|python|perl)", T_UNIX_SHELL, "lolbin-b64-pipe", "Encoded payload piped to interpreter")
_r(r"/dev/tcp/", T_UNIX_SHELL, "lolbin-dev-tcp", "Bash /dev/tcp network access")
_r(r"\bLD_PRELOAD\s*=", T_HIJACK_EXEC_FLOW, "lolbin-ld-preload", "LD_PRELOAD shared library injection")
_r(r"\b(certutil|bitsadmin|mshta|regsvr32|rundll32)\b", T_SIGNED_BINARY, "lolbin-windows", "Windows LOLBin execution")

# --- Credential access (T1552 / T1003) ---
_r(r"\bcat\s+/etc/shadow\b", T_OS_CRED_SHADOW, "cred-shadow", "Reading /etc/shadow")
_r(r"~/\.aws/credentials|\.aws/credentials", T_CRED_CLOUD, "cred-aws", "AWS credential file access")
_r(r"~/\.ssh/(id_|authorized_keys)|\.ssh/(id_|authorized_keys)", T_CRED_SSH, "cred-ssh", "SSH key/authorized_keys access")
_r(r"/var/run/secrets/kubernetes\.io", T_CRED_CONTAINER_API, "cred-k8s-sa", "Kubernetes service account token access")
_r(r"\blsass\b", T_OS_CRED_LSASS, "cred-lsass", "LSASS memory access")

# --- Persistence (T1053 / T1136) ---
# persist-shell-rc (writes to ~/.bashrc) and persist-systemd (systemctl enable
# foo.service) are omitted on purpose: both are legitimate SRE operations that
# the LLM judge is better positioned to evaluate in context.
_r(r"\bcrontab\s+-[ei]", T_SCHED_TASK_CRON, "persist-crontab", "Crontab modification")
_r(r">>\s*.*authorized_keys", T_ACCOUNT_MANIP_SSH, "persist-authkeys", "Appending to authorized_keys")

# --- Defense evasion (T1562) ---
_r(r"\bsetenforce\s+0\b", T_IMPAIR_DEFENSES, "evasion-selinux", "Disabling SELinux")
_r(r"\bauditctl\s+-D\b", T_IMPAIR_DEFENSES, "evasion-auditctl", "Clearing audit rules")
_r(r"\b(ufw\s+disable|iptables\s+-F)\b", T_IMPAIR_FIREWALL, "evasion-firewall", "Disabling firewall")
_r(r"\bhistory\s+-c\b|>\s*~/?\.(bash_history|zsh_history)", T_CLEAR_HISTORY, "evasion-history", "Clearing shell history")
_r(r"\bunset\s+HISTFILE\b", T_CLEAR_HISTORY, "evasion-histfile", "Unsetting HISTFILE")

# --- Reverse shells (T1059) ---
_r(r"\b(?:nc|ncat|netcat)\s+.*-e\s", T_UNIX_SHELL, "revshell-nc", "Netcat reverse shell")
_r(r"\bmkfifo\b.*\bnc\b", T_UNIX_SHELL, "revshell-mkfifo", "Named pipe reverse shell")
_r(r"socket\.socket.*connect.*dup2", T_PYTHON, "revshell-python", "Python reverse shell")
_r(r"\bsocat\b.*TCP[-:]", T_UNIX_SHELL, "revshell-socat", "Socat reverse/bind shell")

# --- Crypto mining (T1496) ---
_r(r"\b(xmrig|cpuminer|minerd)\b", T_RESOURCE_HIJACK, "mining-binary", "Cryptocurrency miner binary")
_r(r"stratum\+tcp://", T_RESOURCE_HIJACK, "mining-stratum", "Mining pool stratum protocol")

# --- Resource destruction ---
_r(r"\brm\s+-(?:rf|fr)\s+/(?:\s|$|['\"])", T_DATA_DESTRUCTION, "destruct-rm-root", "Recursive deletion of root filesystem")
_r(r"\brm\s+-rf\s+/home\b", T_DATA_DESTRUCTION, "destruct-rm-home", "Recursive deletion of /home")
_r(r"\bdd\s+if=/dev/(zero|urandom)\b", T_DISK_WIPE, "destruct-dd", "Disk overwrite with dd")
_r(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", T_ENDPOINT_DOS, "destruct-forkbomb", "Fork bomb")

# --- Privilege escalation (T1548) ---
# Match 4-digit octal modes whose leading bit sets SUID/SGID/both (2/4/6/7),
# or symbolic forms that explicitly add the SUID/SGID bit. Plain 3-digit modes
# like "chmod 755 /etc/..." are routine ops and must not match.
_r(r"\bchmod\s+(?:[2467][0-7]{3}\b|[ugo]*\+[st]\b)", T_ABUSE_ELEVATION, "privesc-chmod-suid", "Setting SUID/SGID bits")


# --- SigmaHQ-sourced rules (loaded from vendored YAML at import time) ---

def _load_sigma() -> None:
    try:
        from utils.security.config import config
        if not config.sigma_enabled:
            logger.debug("Sigma rules disabled via config")
            return
        from utils.security.sigma_loader import load_sigma_rules
        sigma_rules = load_sigma_rules()
        _RULES.extend(sigma_rules)
        logger.info("Signature matcher ready: %d rules (%d hand-written + %d Sigma)", len(_RULES), len(_RULES) - len(sigma_rules), len(sigma_rules))
    except Exception:
        logger.warning("Failed to load Sigma rules, continuing with hand-written rules only", exc_info=True)

_load_sigma()


def check_signature(command: str) -> SignatureVerdict:
    """Run command against all signature rules. Returns on first match."""
    for pattern, technique, rule_id, desc in _RULES:
        if pattern.search(command):
            return SignatureVerdict(matched=True, technique=technique, rule_id=rule_id, description=desc)
    return SignatureVerdict(matched=False)
