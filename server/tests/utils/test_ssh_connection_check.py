"""Check Connection stays up when a network OS rejects whoami."""

import ast
import logging
import os
import pathlib
import socket
import sys
import types

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.ssh.session_reply import (
    CHANNEL_FAILED,
    SESSION_DROPPED,
    session_reply_result,
)

# Pre-checks do not install Paramiko, so this test never imports ssh_utils.
_SSH_UTILS = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "utils" / "ssh" / "ssh_utils.py"
)


def _reply(result="", error="", active=True, channel_error=False, exec_rejected=False):
    return session_reply_result(
        result,
        error,
        "aurora-ro",
        transport_active=active,
        channel_error=channel_error,
        exec_rejected=exec_rejected,
    )


def _load_validate_and_test_ssh(paramiko_mod):
    """Exec just validate_and_test_ssh so the suite does not import Paramiko."""
    tree = ast.parse(_SSH_UTILS.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "validate_and_test_ssh":
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {
                "logger": logging.getLogger("test_ssh_connection_check"),
                "parse_private_key": lambda _key: object(),
                "paramiko": paramiko_mod,
                "session_reply_result": session_reply_result,
                "socket": socket,
                "Optional": __import__("typing").Optional,
            }
            exec(compile(module, str(_SSH_UTILS), "exec"), namespace)  # noqa: S102
            return namespace["validate_and_test_ssh"]
    raise LookupError("validate_and_test_ssh not found")


class _RejectedExecParamiko:
    """Stand-in that authenticates, then refuses the exec channel."""

    class SSHException(Exception):
        pass

    class AuthenticationException(Exception):
        pass

    class WarningPolicy:
        pass

    class _Client:
        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            return None

        def exec_command(self, _command, timeout=5):
            raise _RejectedExecParamiko.SSHException("exec request rejected")

        def get_transport(self):
            return types.SimpleNamespace(is_active=lambda: True)

        def close(self):
            return None

    SSHClient = _Client


def test_matching_whoami_reports_that_user():
    success, error, connected_as = _reply(result="aurora-ro")
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_network_os_error_text_is_success():
    success, error, connected_as = _reply(result="Unknown action 0")
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_invalid_command_on_stderr_is_success():
    success, error, connected_as = _reply(error="% Invalid input detected at '^' marker")
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_channel_timeout_fails_after_login():
    success, error, connected_as = _reply(channel_error=True)
    assert success is False
    assert error == CHANNEL_FAILED
    assert connected_as is None


def test_dead_transport_with_no_reply_fails():
    success, error, connected_as = _reply(active=False)
    assert success is False
    assert error == SESSION_DROPPED
    assert connected_as is None


def test_reply_still_passes_if_transport_closes_after():
    success, error, connected_as = _reply(result="Unknown action 0", active=False)
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_reply_still_passes_if_the_other_stream_errors():
    success, error, _connected_as = _reply(
        result="Unknown action 0",
        channel_error=True,
    )
    assert success is True
    assert error is None


def test_rejected_exec_after_login_is_success():
    success, error, connected_as = _reply(exec_rejected=True)
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_validate_and_test_ssh_accepts_rejected_exec():
    validate = _load_validate_and_test_ssh(_RejectedExecParamiko)
    success, error, connected_as = validate("198.51.100.1", "aurora-ro", "not-a-key")
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"
