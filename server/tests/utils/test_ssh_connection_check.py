"""Check Connection stays up when a network OS rejects whoami."""

import os
import socket
import sys

import paramiko

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.ssh import ssh_utils


class _Stream:
    def __init__(self, payload=b"", error=None):
        self.payload = payload
        self.error = error

    def read(self):
        if self.error:
            raise self.error
        return self.payload


class _Transport:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


class _Client:
    def __init__(
        self,
        stdout=b"",
        stderr=b"",
        read_error=None,
        exec_error=None,
        connect_error=None,
        active=True,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.read_error = read_error
        self.exec_error = exec_error
        self.connect_error = connect_error
        self.active = active

    def set_missing_host_key_policy(self, _policy):
        return None

    def connect(self, **_kwargs):
        if self.connect_error:
            raise self.connect_error

    def exec_command(self, _command, timeout=5):
        if self.exec_error:
            raise self.exec_error
        return (
            None,
            _Stream(self.stdout, self.read_error),
            _Stream(self.stderr),
        )

    def get_transport(self):
        return _Transport(self.active)

    def close(self):
        return None


def _patch(monkeypatch, client):
    monkeypatch.setattr(ssh_utils, "parse_private_key", lambda _key: object())
    monkeypatch.setattr(ssh_utils.paramiko, "SSHClient", lambda: client)


def _check():
    return ssh_utils.validate_and_test_ssh("192.0.2.10", "aurora-ro", "key")


def test_matching_whoami_reports_that_user(monkeypatch):
    _patch(monkeypatch, _Client(stdout=b"aurora-ro\n"))
    success, error, connected_as = _check()
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_network_os_error_text_is_success(monkeypatch):
    _patch(monkeypatch, _Client(stdout=b"Unknown action 0\n"))
    success, error, connected_as = _check()
    assert success is True
    assert error is None
    # The error string is not a username
    assert connected_as == "aurora-ro"


def test_invalid_command_on_stderr_is_success(monkeypatch):
    _patch(monkeypatch, _Client(stderr=b"% Invalid input detected at '^' marker"))
    success, error, connected_as = _check()
    assert success is True
    assert error is None
    assert connected_as == "aurora-ro"


def test_channel_timeout_fails_after_login(monkeypatch):
    _patch(monkeypatch, _Client(read_error=socket.timeout("timed out")))
    success, error, connected_as = _check()
    assert success is False
    assert error == "SSH login succeeded but the session channel failed."
    assert connected_as is None


def test_dead_transport_with_no_reply_fails(monkeypatch):
    _patch(monkeypatch, _Client(active=False))
    success, error, connected_as = _check()
    assert success is False
    assert error == "SSH login succeeded but the session dropped."
    assert connected_as is None


def test_reply_still_passes_if_transport_closes_after(monkeypatch):
    _patch(
        monkeypatch,
        _Client(stdout=b"Unknown action 0\n", active=False),
    )
    success, error, _connected_as = _check()
    assert success is True
    assert error is None


def test_unreachable_host_is_still_a_connection_failure(monkeypatch):
    _patch(monkeypatch, _Client(connect_error=socket.timeout("timed out")))
    success, error, connected_as = _check()
    assert success is False
    assert "timeout" in error.lower()
    assert "login succeeded" not in error.lower()
    assert connected_as is None


def test_rejected_exec_channel_fails(monkeypatch):
    _patch(
        monkeypatch,
        _Client(exec_error=paramiko.SSHException("Channel closed")),
    )
    success, error, connected_as = _check()
    assert success is False
    assert error == "SSH login succeeded but the session channel failed."
    assert connected_as is None
