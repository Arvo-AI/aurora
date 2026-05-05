"""Tests for terminal_exec_tool -- shell-routing helpers, SSH jump rewrite, and entrypoint wiring.

The module pulls in heavy deps (langchain_core, boto3, google.cloud.*), so we
AST-extract pure helpers / the entrypoint and exec them in a controlled
namespace rather than importing the full module.
"""

import ast
import json
import logging
import os
import pathlib
import re
import shlex
import sys
import types
from typing import Optional
from unittest.mock import MagicMock

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
            exec(compile(module, str(_SOURCE_FILE), "exec"), namespace)  # noqa: S102
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

    def test_dash_j_attached_form_handled(self):
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


# ---------------------------------------------------------------------------
# terminal_exec entrypoint -> helpers wiring
# ---------------------------------------------------------------------------


class _StubTerminalRunResult:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _load_terminal_exec_with_spies(transform_spy, metachar_spy):
    """Load ``terminal_exec`` with helper spies and stubbed I/O; returns (func, stubs)."""
    terminal_run_stub = MagicMock(return_value=_StubTerminalRunResult())
    cloud_exec_stub = MagicMock(return_value="{}")
    run_iac_tool_stub = MagicMock(return_value="{}")
    gate_stub = MagicMock(
        return_value=types.SimpleNamespace(allowed=True, code=None, block_reason=""),
    )
    provider_pref_stub = MagicMock(return_value=())

    # ``terminal_exec`` does ``from x import y`` lazily, so seed real module
    # objects in sys.modules before AST-loading the function.
    gate_module = types.ModuleType("utils.auth.command_gate")
    gate_module.gate_command = gate_stub
    sys.modules["utils.auth.command_gate"] = gate_module

    cloud_utils_module = types.ModuleType("utils.cloud.cloud_utils")
    cloud_utils_module.get_provider_preference = provider_pref_stub
    sys.modules["utils.cloud.cloud_utils"] = cloud_utils_module

    func = _load_function(
        "terminal_exec",
        extra_globals={
            "json": json,
            "logger": logging.getLogger("test_terminal_exec"),
            "_transform_ssh_jump_to_proxy": transform_spy,
            "_has_shell_metacharacters": metachar_spy,
            "_build_sanitized_env": lambda: {},
            "terminal_run": terminal_run_stub,
            "cloud_exec": cloud_exec_stub,
            "run_iac_tool": run_iac_tool_stub,
            "Optional": Optional,
        },
    )
    stubs = types.SimpleNamespace(
        terminal_run=terminal_run_stub,
        cloud_exec=cloud_exec_stub,
        run_iac_tool=run_iac_tool_stub,
        gate=gate_stub,
        provider_pref=provider_pref_stub,
    )
    return func, stubs


@pytest.fixture
def cleanup_lazy_module_stubs():
    """Restore sys.modules entries clobbered by ``_load_terminal_exec_with_spies``."""
    saved = {
        key: sys.modules.get(key)
        for key in ("utils.auth.command_gate", "utils.cloud.cloud_utils")
    }
    yield
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


class TestTerminalExecInvokesHelpers:
    """``terminal_exec`` must actually call the routing helpers; spies catch dead-code regressions."""

    def test_ssh_command_calls_transform_ssh_jump_to_proxy(self, cleanup_lazy_module_stubs):
        transform_spy = MagicMock(side_effect=_transform_ssh_jump_to_proxy)
        metachar_spy = MagicMock(side_effect=_has_shell_metacharacters)
        terminal_exec, _ = _load_terminal_exec_with_spies(transform_spy, metachar_spy)

        terminal_exec(
            command="ssh -i /home/me/.ssh/key -J user@bastion user@target",
            user_id="u-1",
            session_id="s-1",
        )

        transform_spy.assert_called_once()
        sent = transform_spy.call_args.args[0]
        assert sent == "ssh -i /home/me/.ssh/key -J user@bastion user@target"

    def test_transformed_ssh_command_is_what_actually_runs(self, cleanup_lazy_module_stubs):
        """The helper's return value -- not the original command -- must reach the gate and runner."""
        transform_spy = MagicMock(side_effect=_transform_ssh_jump_to_proxy)
        metachar_spy = MagicMock(side_effect=_has_shell_metacharacters)
        terminal_exec, stubs = _load_terminal_exec_with_spies(transform_spy, metachar_spy)

        terminal_exec(
            command="ssh -J user@bastion user@target",
            user_id="u-1",
            session_id="s-1",
        )

        executed_cmd = stubs.terminal_run.call_args.args[0]
        assert "ProxyCommand=" in executed_cmd
        assert " -J " not in executed_cmd, "rewrite must remove the -J flag"

        gated_cmd = stubs.gate.call_args.kwargs["command"]
        assert "ProxyCommand=" in gated_cmd

    def test_plain_command_calls_has_shell_metacharacters(self, cleanup_lazy_module_stubs):
        transform_spy = MagicMock(side_effect=_transform_ssh_jump_to_proxy)
        metachar_spy = MagicMock(side_effect=_has_shell_metacharacters)
        terminal_exec, _ = _load_terminal_exec_with_spies(transform_spy, metachar_spy)

        terminal_exec(command="ls -la", user_id="u-1", session_id="s-1")

        metachar_spy.assert_called()
        assert "ls -la" in metachar_spy.call_args.args

    def test_non_ssh_command_skips_ssh_rewrite_helper(self, cleanup_lazy_module_stubs):
        transform_spy = MagicMock(side_effect=_transform_ssh_jump_to_proxy)
        metachar_spy = MagicMock(side_effect=_has_shell_metacharacters)
        terminal_exec, _ = _load_terminal_exec_with_spies(transform_spy, metachar_spy)

        terminal_exec(command="git status", user_id="u-1", session_id="s-1")

        transform_spy.assert_not_called()
        metachar_spy.assert_called()

    def test_gate_decision_blocks_execution(self, cleanup_lazy_module_stubs):
        """A denied gate decision must prevent any runner from executing."""
        transform_spy = MagicMock(side_effect=_transform_ssh_jump_to_proxy)
        metachar_spy = MagicMock(side_effect=_has_shell_metacharacters)
        terminal_exec, stubs = _load_terminal_exec_with_spies(transform_spy, metachar_spy)
        stubs.gate.return_value = types.SimpleNamespace(
            allowed=False, code="POLICY_DENY", block_reason="not allowed",
        )

        result = terminal_exec(command="rm -rf /", user_id="u-1", session_id="s-1")

        stubs.terminal_run.assert_not_called()
        stubs.cloud_exec.assert_not_called()
        stubs.run_iac_tool.assert_not_called()
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert parsed["code"] == "POLICY_DENY"
