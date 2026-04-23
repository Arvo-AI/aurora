"""Static signature matcher (L2) for known-malicious command patterns.

Compiled regex rules run against every command before the LLM judge.
Catches ~80% of dangerous commands in <5ms with zero cost.

Rule categories map to MITRE ATT&CK techniques where applicable.
"""

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class SignatureVerdict:
    matched: bool
    technique: str = ""
    rule_id: str = ""
    description: str = ""


# Each rule: (compiled_pattern, technique, rule_id, description)
_Rule = Tuple[re.Pattern, str, str, str]

_RULES: List[_Rule] = []


def _r(pattern: str, technique: str, rule_id: str, desc: str):
    _RULES.append((re.compile(pattern, re.IGNORECASE), technique, rule_id, desc))


# --- LOLBins / Encoded payload execution (T1059) ---
_r(r"base64\s.*\|\s*(sh|bash|zsh|python|perl)", "T1059.004", "lolbin-b64-pipe", "Encoded payload piped to interpreter")
_r(r"/dev/tcp/", "T1059.004", "lolbin-dev-tcp", "Bash /dev/tcp network access")
_r(r"\bLD_PRELOAD\s*=", "T1574.006", "lolbin-ld-preload", "LD_PRELOAD shared library injection")
_r(r"\b(certutil|bitsadmin|mshta|regsvr32|rundll32)\b", "T1218", "lolbin-windows", "Windows LOLBin execution")

# --- Credential access (T1552 / T1003) ---
_r(r"\bcat\s+/etc/shadow\b", "T1003.008", "cred-shadow", "Reading /etc/shadow")
_r(r"~/\.aws/credentials|\.aws/credentials", "T1552.001", "cred-aws", "AWS credential file access")
_r(r"~/\.ssh/(id_|authorized_keys)|\.ssh/(id_|authorized_keys)", "T1552.004", "cred-ssh", "SSH key/authorized_keys access")
_r(r"/var/run/secrets/kubernetes\.io", "T1552.007", "cred-k8s-sa", "Kubernetes service account token access")
_r(r"\blsass\b", "T1003.001", "cred-lsass", "LSASS memory access")

# --- Persistence (T1053 / T1136) ---
_r(r"\bcrontab\s+-[ei]", "T1053.003", "persist-crontab", "Crontab modification")
_r(r"(>>?\s*~/?\.)?(bashrc|bash_profile|profile|zshrc)\b", "T1546.004", "persist-shell-rc", "Shell RC file modification")
_r(r">>\s*.*authorized_keys", "T1098.004", "persist-authkeys", "Appending to authorized_keys")
_r(r"\bsystemctl\s+(enable|start)\b.*\.(service|timer)", "T1543.002", "persist-systemd", "Systemd unit enable/start")

# --- Defense evasion (T1562) ---
_r(r"\bsetenforce\s+0\b", "T1562.001", "evasion-selinux", "Disabling SELinux")
_r(r"\bauditctl\s+-D\b", "T1562.001", "evasion-auditctl", "Clearing audit rules")
_r(r"\b(ufw\s+disable|iptables\s+-F)\b", "T1562.004", "evasion-firewall", "Disabling firewall")
_r(r"\bhistory\s+-c\b|>\s*~/?\.(bash_history|zsh_history)", "T1070.003", "evasion-history", "Clearing shell history")
_r(r"\bunset\s+HISTFILE\b", "T1070.003", "evasion-histfile", "Unsetting HISTFILE")

# --- Reverse shells (T1059) ---
_r(r"\bnc\s+.*-e\s", "T1059.004", "revshell-nc", "Netcat reverse shell")
_r(r"\bmkfifo\b.*\bnc\b", "T1059.004", "revshell-mkfifo", "Named pipe reverse shell")
_r(r"socket\.socket.*connect.*dup2", "T1059.006", "revshell-python", "Python reverse shell")
_r(r"\bsocat\b.*TCP:", "T1059.004", "revshell-socat", "Socat reverse shell")

# --- Crypto mining (T1496) ---
_r(r"\b(xmrig|cpuminer|minerd)\b", "T1496", "mining-binary", "Cryptocurrency miner binary")
_r(r"stratum\+tcp://", "T1496", "mining-stratum", "Mining pool stratum protocol")

# --- Resource destruction ---
_r(r"\brm\s+-(?:rf|fr)\s+/(?:\s|$)", "T1485", "destruct-rm-root", "Recursive deletion of root filesystem")
_r(r"\brm\s+-rf\s+/home\b", "T1485", "destruct-rm-home", "Recursive deletion of /home")
_r(r"\bdd\s+if=/dev/(zero|urandom)\b", "T1561", "destruct-dd", "Disk overwrite with dd")
_r(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "T1499.004", "destruct-forkbomb", "Fork bomb")

# --- Privilege escalation (T1548) ---
_r(r"\bchmod\s+[0-7]*[2367][0-7]*\s+/", "T1548", "privesc-chmod-suid", "Setting SUID/SGID on system files")


def check_signature(command: str) -> SignatureVerdict:
    """Run command against all signature rules. Returns on first match."""
    for pattern, technique, rule_id, desc in _RULES:
        if pattern.search(command):
            return SignatureVerdict(matched=True, technique=technique, rule_id=rule_id, description=desc)
    return SignatureVerdict(matched=False)
