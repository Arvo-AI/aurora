"""Phase 6 — unit tests for the idle-timeout watchdog.

Mocks the Redis + DB seams so the loop logic is exercised in isolation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


# ---------------------------------------------------------------------------
# Stub heavy third-party packages — same pattern as other test modules.
# ---------------------------------------------------------------------------
for _pkg in ("redis", "redis.asyncio", "psycopg2"):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()


@pytest.fixture(autouse=True)
def _reload_module():
    """Force a fresh import — other tests stub chat_events with MagicMock."""
    import importlib
    for mod in (
        "chat.backend.agent.utils.idle_watchdog",
        "chat.backend.agent.utils.persistence.chat_events",
        "utils.redis.redis_stream_bus",
    ):
        sys.modules.pop(mod, None)
    importlib.import_module("utils.redis.redis_stream_bus")
    importlib.import_module("chat.backend.agent.utils.persistence.chat_events")
    importlib.import_module("chat.backend.agent.utils.idle_watchdog")
    yield


# ---------------------------------------------------------------------------
# refresh_idle_ttl
# ---------------------------------------------------------------------------


def test_refresh_extends_ttl_calls_setex():
    """refresh_idle_ttl must call client.set with ex= ttl_seconds."""
    from chat.backend.agent.utils import idle_watchdog

    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.aclose = AsyncMock()

    async def _fake_get_async_redis():
        return fake_redis

    with patch("utils.redis.redis_stream_bus.get_async_redis", new=_fake_get_async_redis):
        asyncio.run(
            idle_watchdog.refresh_idle_ttl("sess-1", "msg-1", ttl_seconds=42)
        )

    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.call_args
    # Key shape: chat:idle:{session_id}:{message_id}
    assert args[0] == "chat:idle:sess-1:msg-1"
    assert kwargs.get("ex") == 42


def test_refresh_no_redis_is_noop():
    """If Redis is unreachable, refresh_idle_ttl must not raise."""
    from chat.backend.agent.utils import idle_watchdog

    async def _none():
        return None

    with patch("utils.redis.redis_stream_bus.get_async_redis", new=_none):
        # Should not raise.
        asyncio.run(idle_watchdog.refresh_idle_ttl("sess-1", "msg-1"))


def test_refresh_missing_ids_is_noop():
    from chat.backend.agent.utils import idle_watchdog

    asyncio.run(idle_watchdog.refresh_idle_ttl("", "msg"))
    asyncio.run(idle_watchdog.refresh_idle_ttl("sess", ""))


# ---------------------------------------------------------------------------
# check_idle_expiry
# ---------------------------------------------------------------------------


def test_check_idle_expiry_returns_true_when_key_missing():
    from chat.backend.agent.utils import idle_watchdog

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(return_value=0)
    fake_redis.aclose = AsyncMock()

    async def _fake():
        return fake_redis

    with patch("utils.redis.redis_stream_bus.get_async_redis", new=_fake):
        result = asyncio.run(idle_watchdog.check_idle_expiry("s", "m"))
    assert result is True


def test_check_idle_expiry_returns_false_when_key_present():
    from chat.backend.agent.utils import idle_watchdog

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(return_value=1)
    fake_redis.aclose = AsyncMock()

    async def _fake():
        return fake_redis

    with patch("utils.redis.redis_stream_bus.get_async_redis", new=_fake):
        result = asyncio.run(idle_watchdog.check_idle_expiry("s", "m"))
    assert result is False


# ---------------------------------------------------------------------------
# _parse_message_id
# ---------------------------------------------------------------------------


def test_parse_message_id_strips_session_prefix():
    from chat.backend.agent.utils.idle_watchdog import _parse_message_id

    assert _parse_message_id("sid-1:mid-1", "sid-1") == "mid-1"
    assert _parse_message_id("foo:bar", "sid-1") == "bar"
    assert _parse_message_id("", "sid-1") is None


# ---------------------------------------------------------------------------
# _scan_once → end-to-end with mocks
# ---------------------------------------------------------------------------


def test_scan_loop_writes_assistant_failed_on_expiry():
    """Idle key missing for an active session → record_event called with reason=idle_timeout
    and active_stream_id is cleared."""
    from chat.backend.agent.utils import idle_watchdog

    record_calls = []

    async def _fake_record_event(*, session_id, org_id, type, payload, message_id, **kw):
        record_calls.append({
            "session_id": session_id,
            "org_id": org_id,
            "type": type,
            "payload": payload,
            "message_id": message_id,
        })
        return 1

    cleared = []

    def _fake_clear_sync(*, session_id, org_id, expected_stream_id):
        cleared.append((session_id, org_id, expected_stream_id))

    fake_pipe = MagicMock()
    fake_pipe.exists = MagicMock()
    fake_pipe.execute = AsyncMock(return_value=[0])  # 0 == key missing → expired
    fake_redis = MagicMock()
    fake_redis.pipeline = MagicMock(return_value=fake_pipe)
    fake_redis.aclose = AsyncMock()

    async def _fake_get_redis():
        return fake_redis

    fake_streams = [("sess-1", "org-1", "sess-1:msg-A")]

    with patch.object(
        idle_watchdog, "_list_active_streams_sync", return_value=fake_streams
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=_fake_record_event,
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events._clear_active_stream_id_sync",
        new=_fake_clear_sync,
    ), patch(
        "utils.redis.redis_stream_bus.get_async_redis", new=_fake_get_redis
    ):
        n = asyncio.run(idle_watchdog._scan_once())

    assert n == 1
    assert len(record_calls) == 1
    call = record_calls[0]
    assert call["type"] == "assistant_failed"
    assert call["payload"] == {"reason": "idle_timeout"}
    assert call["session_id"] == "sess-1"
    assert call["message_id"] == "msg-A"
    # belt-and-suspenders clear was invoked
    assert cleared == [("sess-1", "org-1", "sess-1:msg-A")]


def test_scan_loop_skips_messages_with_live_idle_key():
    """When the idle key still exists, no failure event is written."""
    from chat.backend.agent.utils import idle_watchdog

    record_calls = []

    async def _fake_record_event(**kw):
        record_calls.append(kw)
        return 1

    fake_pipe = MagicMock()
    fake_pipe.exists = MagicMock()
    fake_pipe.execute = AsyncMock(return_value=[1])  # 1 == key present → alive
    fake_redis = MagicMock()
    fake_redis.pipeline = MagicMock(return_value=fake_pipe)
    fake_redis.aclose = AsyncMock()

    async def _fake_get_redis():
        return fake_redis

    fake_streams = [("sess-1", "org-1", "sess-1:msg-A")]

    with patch.object(
        idle_watchdog, "_list_active_streams_sync", return_value=fake_streams
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=_fake_record_event,
    ), patch(
        "utils.redis.redis_stream_bus.get_async_redis", new=_fake_get_redis
    ):
        n = asyncio.run(idle_watchdog._scan_once())

    assert n == 0
    assert record_calls == []


def test_scan_loop_handles_no_active_streams():
    from chat.backend.agent.utils import idle_watchdog

    with patch.object(idle_watchdog, "_list_active_streams_sync", return_value=[]):
        n = asyncio.run(idle_watchdog._scan_once())
    assert n == 0


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------


def test_idle_ttl_default_is_300_seconds():
    from chat.backend.agent.utils.idle_watchdog import _idle_ttl_seconds

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHAT_IDLE_TIMEOUT_SECONDS", None)
        assert _idle_ttl_seconds() == 300


def test_idle_ttl_respects_env_override():
    from chat.backend.agent.utils.idle_watchdog import _idle_ttl_seconds

    with patch.dict(os.environ, {"CHAT_IDLE_TIMEOUT_SECONDS": "75"}):
        assert _idle_ttl_seconds() == 75


def test_idle_ttl_falls_back_on_invalid_env():
    from chat.backend.agent.utils.idle_watchdog import _idle_ttl_seconds

    with patch.dict(os.environ, {"CHAT_IDLE_TIMEOUT_SECONDS": "garbage"}):
        assert _idle_ttl_seconds() == 300
