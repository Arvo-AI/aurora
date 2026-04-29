"""Tests for routes/chat_sse.py — Phase 5B SSE transport.

Covered:
  * GET /api/chat/stream → 204 when no active stream and no replay backlog.
  * GET /api/chat/stream → SSE frames + meta:resumed + meta:completed
    when a replay backlog exists with a terminal event.
  * Frame formatting (event:, data:, id:) for one chat_events row.

These tests exercise the SSE generator's pure logic by mocking the
chat_events / redis_stream_bus seams. They do NOT spin up Flask, Redis, or
Postgres — Phase 6 will add an integration-level test.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


@pytest.fixture(autouse=True)
def _reload_real_chat_events_module():
    """Other test modules (test_subagent_failure.py /
    test_subagent_parallel_safety.py) install MagicMock stubs into
    ``sys.modules['chat.backend.agent.utils.persistence.chat_events']`` without
    cleanup. That leaks across the session and pollutes ``routes.chat_sse``'s
    ``EVENT_TYPES`` / ``fetch_events_after`` bindings (a MagicMock makes
    ``x in EVENT_TYPES`` return False by default, which silently filters out
    every backlog event). Force a clean re-import before each test."""
    for mod in (
        "chat.backend.agent.utils.persistence.chat_events",
        "utils.redis.redis_stream_bus",
        "routes.chat_sse",
    ):
        sys.modules.pop(mod, None)
    importlib.import_module("chat.backend.agent.utils.persistence.chat_events")
    importlib.import_module("utils.redis.redis_stream_bus")
    importlib.import_module("routes.chat_sse")
    yield


# ---------------------------------------------------------------------------
# _format_frame
# ---------------------------------------------------------------------------


def test_format_frame_includes_event_data_and_id():
    from routes.chat_sse import _format_frame

    out = _format_frame("assistant_chunk", {"seq": 7, "text": "hello"}, seq=7)
    decoded = out.decode("utf-8")
    assert decoded.startswith("event: assistant_chunk\n")
    assert "data: " in decoded
    # JSON payload survives round-trip
    data_line = next(
        line for line in decoded.split("\n") if line.startswith("data: ")
    )
    parsed = json.loads(data_line[len("data: "):])
    assert parsed["text"] == "hello"
    assert "id: 7" in decoded
    assert decoded.endswith("\n\n")


def test_format_frame_skips_id_when_none():
    from routes.chat_sse import _format_frame

    out = _format_frame("meta:resumed", {"resumed_from": 5}, seq=None).decode("utf-8")
    assert "event: meta:resumed" in out
    assert "id: " not in out


# ---------------------------------------------------------------------------
# _drive_sse — replay backlog with a terminal event closes the stream
# ---------------------------------------------------------------------------


def test_drive_sse_replays_backlog_and_closes_on_terminal(monkeypatch):
    """A backlog containing assistant_started + assistant_chunk + assistant_finalized
    should yield three event frames, a meta:resumed marker, and meta:completed."""
    from routes import chat_sse

    backlog = [
        {
            "seq": 1, "type": "assistant_started",
            "payload": {"mode": "ask"}, "agent_id": "main",
            "parent_agent_id": None, "message_id": "m1",
        },
        {
            "seq": 2, "type": "assistant_chunk",
            "payload": {"text": "hi"}, "agent_id": "main",
            "parent_agent_id": None, "message_id": "m1",
        },
        {
            "seq": 3, "type": "assistant_finalized",
            "payload": {"text": "hi"}, "agent_id": "main",
            "parent_agent_id": None, "message_id": "m1",
        },
    ]

    async def _fake_fetch(*, session_id, org_id, after_seq, limit):
        return [ev for ev in backlog if ev["seq"] > after_seq]

    monkeypatch.setattr(chat_sse, "fetch_events_after", _fake_fetch)

    import asyncio
    loop = asyncio.new_event_loop()
    try:
        frames = list(
            chat_sse._drive_sse(
                loop=loop,
                session_id="s1",
                org_id="org-1",
                last_event_id=0,
            )
        )
    finally:
        loop.close()

    decoded = b"".join(frames).decode("utf-8")
    assert "event: assistant_started" in decoded
    assert "event: assistant_chunk" in decoded
    assert "event: assistant_finalized" in decoded
    assert "event: meta:resumed" in decoded
    assert "event: meta:completed" in decoded
    # id: lines must use chat_events.seq, not redis entry id
    assert "id: 1" in decoded
    assert "id: 2" in decoded
    assert "id: 3" in decoded


# ---------------------------------------------------------------------------
# _has_anything_to_stream  — 204 path
# ---------------------------------------------------------------------------


def test_has_anything_to_stream_returns_false_when_empty(monkeypatch):
    from routes import chat_sse

    async def _fake_active(*, session_id, org_id):
        return None

    async def _fake_fetch(*, session_id, org_id, after_seq, limit):
        return []

    monkeypatch.setattr(chat_sse, "get_active_stream_id", _fake_active)
    monkeypatch.setattr(chat_sse, "fetch_events_after", _fake_fetch)

    has_active, backlog = chat_sse._has_anything_to_stream("s", "org", 0)
    assert has_active is False
    assert backlog is False


def test_has_anything_to_stream_returns_true_when_backlog(monkeypatch):
    from routes import chat_sse

    async def _fake_active(*, session_id, org_id):
        return None

    async def _fake_fetch(*, session_id, org_id, after_seq, limit):
        return [{"seq": 1, "type": "user_message", "payload": {}}]

    monkeypatch.setattr(chat_sse, "get_active_stream_id", _fake_active)
    monkeypatch.setattr(chat_sse, "fetch_events_after", _fake_fetch)

    has_active, backlog = chat_sse._has_anything_to_stream("s", "org", 0)
    assert has_active is False
    assert backlog is True


# ---------------------------------------------------------------------------
# Wire-data shape contract
# ---------------------------------------------------------------------------


def test_wire_data_shape_matches_cross_agent_contract():
    from routes.chat_sse import _wire_data

    d = _wire_data(
        seq=42,
        session_id="sess",
        type_="tool_call_started",
        payload={"tool": "x"},
        message_id="msg",
        agent_id="sub-1",
        parent_agent_id="main",
    )
    # The cross-agent contract requires exactly these keys.
    assert set(d.keys()) == {
        "seq", "session_id", "message_id", "agent_id",
        "parent_agent_id", "type", "payload",
    }
    assert d["seq"] == 42
    assert d["payload"] == {"tool": "x"}
