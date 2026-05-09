"""Tests the X-Internal-Secret gate on the internal kubectl HTTP endpoint.

The endpoint must reject every request missing the exact configured secret —
absent, wrong, byte-flipped, empty, and wrong-case values all produce a 403.
Also verifies the comparison uses ``hmac.compare_digest`` for constant-time safety.
"""

import asyncio
import hmac
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.internal import api_handler as _api_handler_module
from utils.internal.api_handler import _handle_kubectl_execute  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REAL_SECRET = "s3cr3t-internal-token"


def _writer_spy():
    """Async writer whose ``write`` captures all written bytes."""
    spy = MagicMock()
    spy.drain = AsyncMock()
    spy.write = MagicMock()
    return spy


def _last_json(writer) -> dict:
    """Decode the last JSON payload written to *writer*."""
    all_bytes = b"".join(
        call.args[0]
        for call in writer.write.call_args_list
        if call.args
    )
    body_start = all_bytes.find(b"\r\n\r\n") + 4
    return json.loads(all_bytes[body_start:].decode())


def _valid_body(user_id="u-1", cluster_id="cluster-1", command="kubectl get pods"):
    return json.dumps(
        {"user_id": user_id, "cluster_id": cluster_id, "command": command}
    ).encode()


# ---------------------------------------------------------------------------
# Fixture: environment with the secret configured
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_env_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _REAL_SECRET)


# ---------------------------------------------------------------------------
# Tests: missing or wrong secret → 403
# ---------------------------------------------------------------------------


class TestInternalSecretGate:
    """The secret gate must refuse every call that lacks the correct credential."""

    def test_missing_secret_header_returns_403(self):
        """No ``X-Internal-Secret`` header at all → 403 Forbidden."""
        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") == "Unauthorized", (
            "Missing X-Internal-Secret must return {\"error\": \"Unauthorized\"}. "
            f"Got: {response!r}"
        )

    def test_wrong_secret_returns_403(self):
        """A plausible but incorrect secret must be refused."""
        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": "wrong-secret"},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") == "Unauthorized", (
            "Wrong X-Internal-Secret must return {\"error\": \"Unauthorized\"}. "
            f"Got: {response!r}"
        )

    def test_byte_flipped_secret_returns_403(self):
        """A one-character deviation from the real secret must be refused.

        Guards against off-by-one accidents in the comparison logic.
        """
        flipped = _REAL_SECRET[:-1] + chr(ord(_REAL_SECRET[-1]) ^ 1)
        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": flipped},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") == "Unauthorized", (
            f"Byte-flipped secret {flipped!r} must be rejected. Got: {response!r}"
        )

    def test_empty_string_secret_returns_403(self):
        """An explicit empty ``X-Internal-Secret`` header must be refused."""
        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": ""},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") == "Unauthorized", (
            "Empty X-Internal-Secret must be rejected. Got: {response!r}"
        )

    def test_case_sensitive_secret_comparison(self):
        """Secret comparison is byte-exact; wrong case must fail."""
        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": _REAL_SECRET.upper()},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") == "Unauthorized", (
            "Upper-cased secret must be rejected (comparison is byte-exact). "
            f"Got: {response!r}"
        )


# ---------------------------------------------------------------------------
# Tests: comparison must be constant-time
# ---------------------------------------------------------------------------


class TestConstantTimeComparison:
    """The gate must use ``hmac.compare_digest``, never ``==``."""

    def test_secret_check_calls_compare_digest(self, monkeypatch):
        """The implementation must delegate to ``hmac.compare_digest``
        so the runtime does not leak the secret length or prefix via timing.
        """
        compare_spy = MagicMock(wraps=hmac.compare_digest)
        monkeypatch.setattr(_api_handler_module.hmac, "compare_digest", compare_spy)

        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": "attacker-value"},
                body=_valid_body(),
                writer=writer,
            )
        )

        assert compare_spy.called, (
            "Secret comparison must go through hmac.compare_digest "
            "to guarantee constant-time behaviour."
        )


# ---------------------------------------------------------------------------
# Tests: correct secret passes the gate
# ---------------------------------------------------------------------------


class TestCorrectSecretAllowed:
    """Control case: the right secret reaches the handler body."""

    def test_correct_secret_proceeds_past_gate(self, monkeypatch):
        """Correct ``X-Internal-Secret`` must not trigger a 403.

        We stub out the downstream websocket handler module (and its
        ``websockets`` transitive dependency) before the coroutine runs so
        the handler can complete without any real infrastructure.
        """
        ws_handler_stub = MagicMock()
        ws_handler_stub.get_agent_websocket_by_cluster.return_value = None
        ws_handler_stub.register_command_response_handler = MagicMock()
        ws_handler_stub.unregister_command_response_handler = MagicMock()

        monkeypatch.setitem(sys.modules, "websockets", MagicMock())
        monkeypatch.setitem(sys.modules, "utils.kubectl.agent_ws_handler", ws_handler_stub)

        writer = _writer_spy()
        asyncio.run(
            _handle_kubectl_execute(
                headers={"x-internal-secret": _REAL_SECRET},
                body=_valid_body(),
                writer=writer,
            )
        )

        response = _last_json(writer)
        assert response.get("error") != "Unauthorized", (
            "The correct secret must not produce a 403. "
            f"Got: {response!r}"
        )
