"""Tests for terminal_exec_tool -- shell-routing and SSH jump rewrite.

``terminal_exec_tool`` imports heavy deps (langchain_core, boto3,
google.cloud.*).  We AST-extract the pure helpers and exec them into a
controlled namespace to skip the import graph.
"""

import ast
import os
import pathlib
import re
import shlex
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


_SOURCE_FILE = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "chat" / "backend" / "agent" / "tools" / "terminal_exec_tool.py"
)


def _load_function(name: str, extra_globals: dict | None = None):
    """Extract a top-level function from the source file by name."""
    tree = ast.parse(_SOURCE_FILE.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = dict(extra_globals or {})
            exec(compile(module, str(_SOURCE_FILE), "exec"), namespace)
            return namespace[name]
    raise LookupError(f"function {name!r} not found in {_SOURCE_FILE}")


_has_shell_metacharacters = _load_function("_has_shell_metacharacters")
_transform_ssh_jump_to_proxy = _load_function(
    "_transform_ssh_jump_to_proxy",
    extra_globals={"re": re, "shlex": shlex},
)


# ---------------------------------------------------------------------------
# _has_shell_metacharacters
# ---------------------------------------------------------------------------


class TestPlainCommandsReturnFalse:
    """Routine SRE commands must NOT be flagged as needing a shell."""

    @pytest.mark.parametrize("cmd", [
        "ls",
        "ls -la",
        "kubectl get pods",
        "kubectl describe pod nginx-abc123",
        "aws s3 ls",
        "aws ec2 describe-instances --region us-east-1",
        "gcloud compute instances list",
        "echo hello",
        "cat /etc/hosts",
        "git status",
        "terraform plan",
        "docker ps",
    ])
    def test_plain_command_returns_false(self, cmd):
        assert _has_shell_metacharacters(cmd) is False


class TestMetacharactersTriggerTrue:
    """One positive case per metacharacter in the patterns list."""

    @pytest.mark.parametrize("cmd", [
        "cat foo | grep bar",
        "cmd1 || cmd2",
        "cmd1 && cmd2",
        "cmd1; cmd2",
        "echo $(whoami)",
        "echo `whoami`",
        "cmd 2>err.log",
        "cmd 2>&1",
        "cmd > out.log",
        "cmd >> out.log",
        "cmd < in.txt",
        "cmd1 & cmd2",
    ])
    def test_metacharacter_triggers_true(self, cmd):
        assert _has_shell_metacharacters(cmd) is True


class TestRedirectsAtCommandStart:
    """A command starting with a redirect must route through a shell."""

    @pytest.mark.parametrize("cmd", [
        ">foo",
        ">>foo",
        "<foo",
        "2>foo",
        "   >foo",
    ])
    def test_redirect_prefix_returns_true(self, cmd):
        assert _has_shell_metacharacters(cmd) is True


class TestPinnedBehavior:
    """Lock down current behaviour for cases the design doc calls out.

    These document current behaviour, not necessarily the desired
    posture.  If the gate is later tightened (e.g. to catch newlines),
    update these tests as part of that change.
    """

    def test_newline_is_not_a_metacharacter(self):
        assert _has_shell_metacharacters("cmd1\ncmd2") is False

    @pytest.mark.parametrize("cmd", [
        "ls *.txt",
        "cat ?.log",
        "ls [abc].txt",
    ])
    def test_globs_are_not_metacharacters(self, cmd):
        assert _has_shell_metacharacters(cmd) is False

    def test_redirect_without_spaces_in_middle_not_flagged(self):
        assert _has_shell_metacharacters("cmd>foo") is False

    def test_amp_without_spaces_not_flagged(self):
        assert _has_shell_metacharacters("cmd1&cmd2") is False

    def test_empty_string_returns_false(self):
        assert _has_shell_metacharacters("") is False


# ---------------------------------------------------------------------------
# _transform_ssh_jump_to_proxy
# ---------------------------------------------------------------------------


class TestNoOpPaths:
    """Cases where the function has nothing to do return the input verbatim."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "kubectl get pods",
        "echo ssh -J foo",
        "scp -J u@b file user@target:/home/me/",
        "",
    ])
    def test_non_ssh_command_unchanged(self, cmd):
        assert _transform_ssh_jump_to_proxy(cmd) == cmd

    @pytest.mark.parametrize("cmd", [
        "ssh user@target",
        "ssh -i /home/me/.ssh/key user@target",
        "ssh -p 2222 user@target ls -la",
        "ssh -o StrictHostKeyChecking=no user@target",
    ])
    def test_ssh_without_jump_flag_unchanged(self, cmd):
        assert _transform_ssh_jump_to_proxy(cmd) == cmd

    def test_bare_ssh_token_unchanged(self):
        assert _transform_ssh_jump_to_proxy("ssh") == "ssh"


class TestDocumentedTransformations:
    """Each documented input maps to its exact expected output."""

    def test_full_form_with_identity_and_remote_command(self):
        cmd = "ssh -i /home/me/.ssh/key -J user@bastion user@target ls"
        expected = (
            'ssh -i /home/me/.ssh/key '
            '-o ProxyCommand="ssh -i /home/me/.ssh/key '
            '-o StrictHostKeyChecking=no '
            '-o UserKnownHostsFile=/dev/null '
            '-W %h:%p user@bastion -p 22" '
            'user@target ls'
        )
        assert _transform_ssh_jump_to_proxy(cmd) == expected

    def test_dash_J_attached_form_handled(self):
        """`-Juser@bastion` (no space) is treated like `-J user@bastion`."""
        cmd = "ssh -Juser@bastion user@target"
        expected = (
            'ssh -o ProxyCommand="ssh '
            '-o StrictHostKeyChecking=no '
            '-o UserKnownHostsFile=/dev/null '
            '-W %h:%p user@bastion -p 22" '
            'user@target'
        )
        assert _transform_ssh_jump_to_proxy(cmd) == expected

    def test_jump_spec_without_user_part(self):
        """`-J bastion` (no `user@`) keeps the bastion bare in ProxyCommand."""
        out = _transform_ssh_jump_to_proxy("ssh -J bastion u@target")
        assert "-W %h:%p bastion -p 22" in out
        assert "@bastion" not in out


class TestIdentityFilePreserved:
    """Identity file (-i) must propagate to BOTH outer ssh and ProxyCommand."""

    def test_identity_file_appears_on_outer_and_proxy(self):
        out = _transform_ssh_jump_to_proxy("ssh -i /home/me/.ssh/id_rsa -J u@bastion u@target")
        assert out.startswith("ssh -i /home/me/.ssh/id_rsa ")
        assert 'ProxyCommand="ssh -i /home/me/.ssh/id_rsa ' in out

    def test_attached_identity_form(self):
        """`-i/home/me/.ssh/key` (no space) parses identically to `-i /home/me/.ssh/key`."""
        out = _transform_ssh_jump_to_proxy("ssh -i/home/me/.ssh/id_rsa -J u@bastion u@target")
        assert out.startswith("ssh -i /home/me/.ssh/id_rsa ")
        assert 'ProxyCommand="ssh -i /home/me/.ssh/id_rsa ' in out


class TestPortHandling:
    """Jump-host port and target port must not get confused."""

    def test_jump_port_in_proxycommand_target_port_in_outer(self):
        out = _transform_ssh_jump_to_proxy("ssh -p 22 -J user@bastion:2200 user@target")
        assert "-W %h:%p user@bastion -p 2200" in out
        assert " -p 22 user@target" in out

    def test_target_with_embedded_port_kept_verbatim(self):
        """`-J user@bastion:2200 user@target:22` -- target string preserved as-given."""
        cmd = "ssh -J user@bastion:2200 user@target:22"
        expected = (
            'ssh -o ProxyCommand="ssh '
            '-o StrictHostKeyChecking=no '
            '-o UserKnownHostsFile=/dev/null '
            '-W %h:%p user@bastion -p 2200" '
            'user@target:22'
        )
        assert _transform_ssh_jump_to_proxy(cmd) == expected

    def test_jump_spec_default_port_is_22(self):
        out = _transform_ssh_jump_to_proxy("ssh -J u@bastion u@target")
        assert "-W %h:%p u@bastion -p 22" in out


class TestRobustness:
    """Malformed or quoted-path inputs must not raise."""

    def test_unclosed_quote_returns_original(self):
        """`shlex.split` raises on unclosed quotes; function catches and passes through."""
        cmd = 'ssh -J u@b u@t "unclosed'
        assert _transform_ssh_jump_to_proxy(cmd) == cmd

    def test_identity_path_with_space_does_not_raise(self):
        result = _transform_ssh_jump_to_proxy('ssh -i "/home/me/my key" -J u@b u@t')
        assert isinstance(result, str)
        assert "/home/me/my key" in result
        assert 'ProxyCommand="' in result

    def test_identity_path_with_embedded_quote_does_not_raise(self):
        result = _transform_ssh_jump_to_proxy("ssh -i '/home/me/weird\"name' -J u@b u@t")
        assert isinstance(result, str)
        assert len(result) > 0
