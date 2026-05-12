"""Tests for the chat_with_aurora MCP bridge."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from aurora_mcp import chat_bridge


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Squash poll intervals so tests don't actually sleep."""
    monkeypatch.setattr(chat_bridge, "_POLL_INTERVAL_INITIAL", 0.001)
    monkeypatch.setattr(chat_bridge, "_POLL_INTERVAL_MAX", 0.001)
    monkeypatch.setattr(chat_bridge, "_POLL_TOTAL_SECONDS", 0.02)


def _make_api_call(script: List[Dict[str, Any]]):
    """Return an api_call that consumes responses from `script` in order."""
    calls: List[tuple] = []

    async def _api(method, path, *, params=None, body=None, timeout=30):
        calls.append((method, path, params, body))
        return script.pop(0) if script else {"error": "no more scripted responses"}

    return _api, calls


def test_chat_creates_session_and_polls_until_complete():
    api_call, calls = _make_api_call([
        {"id": "sess-123"},                                          # POST /chat_api/sessions
        {"session_id": "sess-123", "seq": 1, "status": "in_progress"},  # POST messages
        {"status": "in_progress", "messages": [], "seq": 1},         # poll 1 (no new msgs)
        {"status": "complete", "messages": [
            {"sender": "aurora", "text": "Here's what I found."}
        ], "seq": 2, "citations": ["c1"]},                            # poll 2: done
    ])
    result = asyncio.run(chat_bridge.chat_with_aurora(api_call, message="hello"))
    assert result["status"] == "complete"
    assert result["response"] == "Here's what I found."
    assert result["session_id"] == "sess-123"
    assert result["citations"] == ["c1"]

    assert calls[0][:2] == ("POST", "/chat_api/sessions")
    assert calls[1][:2] == ("POST", "/chat_api/sessions/sess-123/messages")
    assert calls[2][:2] == ("GET", "/chat_api/sessions/sess-123/messages")


def test_chat_resumes_existing_session_without_creating():
    api_call, calls = _make_api_call([
        {"session_id": "abc", "seq": 5, "status": "in_progress"},
        {"status": "complete", "messages": [
            {"sender": "aurora", "text": "Follow-up answer."}
        ], "seq": 6},
    ])
    result = asyncio.run(chat_bridge.chat_with_aurora(
        api_call, message="follow up", session_id="abc",
    ))
    assert result["status"] == "complete"
    # Notably no POST /chat_api/sessions call.
    assert calls[0][:2] == ("POST", "/chat_api/sessions/abc/messages")


def test_chat_returns_in_progress_when_poll_times_out():
    api_call, _ = _make_api_call([
        {"id": "sess-999"},
        {"session_id": "sess-999", "seq": 1, "status": "in_progress"},
        # Force the poll loop to never see a complete status.
        *[{"status": "in_progress", "messages": [], "seq": 1}] * 50,
    ])
    result = asyncio.run(chat_bridge.chat_with_aurora(api_call, message="long task"))
    assert result["status"] == "in_progress"
    assert result["session_id"] == "sess-999"
    assert "hint" in result


def test_chat_poll_only_resumes_without_posting():
    api_call, calls = _make_api_call([
        {"status": "complete", "messages": [
            {"sender": "aurora", "text": "Done."}
        ], "seq": 7},
    ])
    result = asyncio.run(chat_bridge.chat_with_aurora(
        api_call, session_id="sess-789", poll_only=True,
    ))
    assert result["status"] == "complete"
    # No POST /messages — only the GET poll happened.
    assert calls[0][0] == "GET"


def test_poll_only_requires_session_id():
    async def _api(*a, **k): return {}
    result = asyncio.run(chat_bridge.chat_with_aurora(
        _api, poll_only=True,
    ))
    assert "error" in result


def test_chat_validates_mode():
    async def _api(*a, **k): return {}
    result = asyncio.run(chat_bridge.chat_with_aurora(
        _api, message="hi", mode="bogus",
    ))
    assert result["error"]


def test_chat_returns_error_status_on_backend_failure():
    api_call, _ = _make_api_call([
        {"id": "sess-err"},
        {"session_id": "sess-err", "seq": 1, "status": "in_progress"},
        {"status": "error", "messages": [], "error": "agent crashed"},
    ])
    result = asyncio.run(chat_bridge.chat_with_aurora(api_call, message="fail"))
    assert result["status"] == "error"
    assert result["error"] == "agent crashed"
