"""Tests for cross-agent fingerprint dedup (orchestrator/dedup.py + capture)."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages that pull network/LLM deps on import.
for _pkg in (
    "langchain_core",
    "langchain_core.messages",
    "langchain_core.callbacks",
):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

from chat.backend.agent.orchestrator import dedup as dedup_mod  # noqa: E402
from chat.backend.agent.orchestrator.dedup import (  # noqa: E402
    _check_sync,
    _store_sync,
    compute_fingerprint,
)


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_canonicalisation():
    """Args dict order must not change the fingerprint."""
    f1 = compute_fingerprint("X", {"a": 1, "b": 2})
    f2 = compute_fingerprint("X", {"b": 2, "a": 1})
    assert f1 == f2


def test_fingerprint_distinct_by_tool_name():
    """Same args, different tool name → different fingerprint."""
    f1 = compute_fingerprint("tool_a", {"x": 1})
    f2 = compute_fingerprint("tool_b", {"x": 1})
    assert f1 != f2


def test_fingerprint_distinct_by_args():
    """Same tool, different args → different fingerprint."""
    f1 = compute_fingerprint("tool_a", {"x": 1})
    f2 = compute_fingerprint("tool_a", {"x": 2})
    assert f1 != f2


# ---------------------------------------------------------------------------
# _check_sync
# ---------------------------------------------------------------------------


def test_check_sync_returns_none_on_miss():
    """Redis returns None → _check_sync returns None."""
    fake_client = MagicMock()
    fake_client.get.return_value = None
    with patch.object(dedup_mod, "get_redis_client", return_value=fake_client):
        assert _check_sync("inc-1", "fp-1") is None


def test_check_sync_returns_dict_on_hit():
    """Redis returns JSON → _check_sync deserializes to dict."""
    payload = {"tool_name": "X", "result_preview": "abc", "agent_id": "sub-1"}
    fake_client = MagicMock()
    fake_client.get.return_value = json.dumps(payload).encode("utf-8")
    with patch.object(dedup_mod, "get_redis_client", return_value=fake_client):
        result = _check_sync("inc-1", "fp-1")
    assert result == payload


def test_check_sync_returns_none_when_client_unavailable():
    """If get_redis_client returns None, _check_sync must return None."""
    with patch.object(dedup_mod, "get_redis_client", return_value=None):
        assert _check_sync("inc-1", "fp-1") is None


# ---------------------------------------------------------------------------
# _store_sync
# ---------------------------------------------------------------------------


def test_store_sync_truncates_result_preview():
    """store_fingerprint truncates result_preview to <= 4096 chars before SET."""
    fake_client = MagicMock()
    huge_preview = "x" * 10000
    value = {"tool_name": "X", "agent_id": "sub-1", "result_preview": huge_preview}
    import asyncio

    from chat.backend.agent.orchestrator.dedup import store_fingerprint

    with patch.object(dedup_mod, "get_redis_client", return_value=fake_client):
        asyncio.run(store_fingerprint("inc-1", "fp-1", value, ttl=60))

    fake_client.set.assert_called_once()
    args, _ = fake_client.set.call_args
    # second positional arg is the JSON-encoded value
    written = json.loads(args[1])
    assert len(written["result_preview"]) <= 4096


# ---------------------------------------------------------------------------
# Redis failures must not crash callers
# ---------------------------------------------------------------------------


def test_redis_failure_swallowed():
    """Redis raises → _check_sync returns None, _store_sync does not raise."""
    fake_client = MagicMock()
    fake_client.get.side_effect = RuntimeError("redis down")
    fake_client.set.side_effect = RuntimeError("redis down")
    with patch.object(dedup_mod, "get_redis_client", return_value=fake_client):
        assert _check_sync("inc-1", "fp-1") is None
        # _store_sync must not raise
        _store_sync("inc-1", "fp-1", {"result_preview": "x"}, ttl=60)


# ---------------------------------------------------------------------------
# ToolContextCapture short-circuit on dedup hit (B3-A regression test)
# ---------------------------------------------------------------------------


def test_capture_short_circuits_on_dedup_hit():
    """capture_tool_start must NOT raise on hit; it stashes a deduped marker.

    Regression test for the B3-A bug where a hit raised ToolDedupHit and
    crashed the streaming callback path.
    """
    # Stub the heavy modules that ToolContextCapture imports at module load.
    for _pkg in (
        "tiktoken",
        "openai",
        "anthropic",
    ):
        if _pkg not in sys.modules:
            sys.modules[_pkg] = MagicMock()

    # Patch DB + RLS + tracker so __init__ doesn't touch real resources.
    with patch("utils.db.connection_pool.db_pool") as fake_pool, patch(
        "utils.auth.stateless_auth.set_rls_context", return_value="org-1"
    ):
        fake_pool.get_admin_connection.return_value.__enter__.return_value = MagicMock()

        try:
            from chat.backend.agent.utils.tool_context_capture import (
                ToolContextCapture,
            )
        except Exception as e:  # pragma: no cover - import gating
            pytest.skip(f"ToolContextCapture import failed in sandbox: {e}")

        capture = ToolContextCapture(
            session_id="sess-1",
            user_id="user-1",
            incident_id="inc-1",
            org_id="org-1",
            agent_id="main",
        )

    # Now patch the dedup probe to simulate a hit.
    cached_payload = {
        "tool_name": "tool_x",
        "agent_id": "sub-prev",
        "result_preview": "PREVIOUS_RESULT",
    }
    with patch(
        "chat.backend.agent.orchestrator.dedup._check_sync",
        return_value=cached_payload,
    ), patch(
        "chat.backend.agent.orchestrator.dedup.compute_fingerprint",
        return_value="fp-1",
    ), patch.object(capture, "_record_step_start", return_value=None), patch.object(
        capture, "_emit_chat_event"
    ):
        # given a dedup hit, when capture_tool_start runs, then it must NOT raise
        capture.capture_tool_start("tool_x", {"a": 1}, tool_call_id="t1")

    entry = capture.current_tool_calls.get("t1", {})
    assert entry.get("deduped") is True
    assert entry.get("cached_result") == "PREVIOUS_RESULT"
