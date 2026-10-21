"""Check Connection stays up when a network OS rejects whoami."""

import os
import sys

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.ssh.session_reply import (
    CHANNEL_FAILED,
    SESSION_DROPPED,
    session_reply_result,
)


def _reply(result="", error="", active=True, channel_error=False):
    return session_reply_result(
        result,
        error,
        "aurora-ro",
        transport_active=active,
        channel_error=channel_error,
    )


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
