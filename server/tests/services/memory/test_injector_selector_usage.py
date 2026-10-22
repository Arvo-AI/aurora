"""Memory selector usage tracking stays off the per-turn critical path
(services.memory.injector._select_relevant_memories_async).

The selector runs on every chat turn inside a prefetch bounded to a few seconds;
its ``llm_usage_tracking`` row (tiktoken count + DB insert with RLS set-up) must
be written in the background so a slow database can never time the prefetch out
and drop the memories the model already picked (review on #740).
"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.memory import injector


_ENTRIES = [
    {"category": "runbook", "title": "Redis", "description": "redis runbook", "updated_at": datetime(2024, 1, 1)},
    {"category": "context", "title": "Slack", "description": None, "updated_at": None},
]


class _FakeLLM:
    def __init__(self, content):
        self.content = content

    async def ainvoke(self, messages, config=None):
        await asyncio.sleep(0)
        return SimpleNamespace(content=self.content)


@pytest.fixture
def tracker_calls(monkeypatch):
    """Replace the blocking tracker with a recorder of (user_id, session_id, response)."""
    calls = []

    def _track(messages, response, start_time, user_id, session_id):
        calls.append({"user_id": user_id, "session_id": session_id, "response": response})

    monkeypatch.setattr(injector, "_track_selector_usage", _track)
    return calls


def _select(monkeypatch, content, **kwargs):
    """Run the selector on a fake model; returns (selected, background tasks pending
    right after the selection came back). The tasks are flushed before returning."""
    monkeypatch.setattr(injector, "get_cached_chat_model", lambda *a, **k: _FakeLLM(content))

    async def _go():
        selected = await injector._select_relevant_memories_async("why is redis slow", _ENTRIES, **kwargs)
        pending = len(injector._background_tracking)
        await asyncio.gather(*injector._background_tracking)
        return selected, pending

    return asyncio.run(_go())


def test_selection_is_returned_before_the_usage_row_is_written(monkeypatch, tracker_calls):
    content = json.dumps({"selected": [{"category": "runbook", "title": "Redis"}]})
    monkeypatch.setattr(injector, "get_cached_chat_model", lambda *a, **k: _FakeLLM(content))

    async def _go():
        selected = await injector._select_relevant_memories_async(
            "why is redis slow", _ENTRIES, user_id="u-1", session_id="s-1"
        )
        # The task exists but has not run yet: nothing sat between the model answer
        # and the returned selection.
        snapshot = (len(injector._background_tracking), len(tracker_calls))
        await asyncio.gather(*injector._background_tracking)
        return selected, snapshot

    selected, (pending, recorded_before_flush) = asyncio.run(_go())

    assert [e["title"] for e in selected] == ["Redis"]
    assert pending == 1
    assert recorded_before_flush == 0
    assert len(tracker_calls) == 1
    assert (tracker_calls[0]["user_id"], tracker_calls[0]["session_id"]) == ("u-1", "s-1")
    assert tracker_calls[0]["response"].content == content
    assert injector._background_tracking == set()


def test_nothing_is_tracked_without_a_user_id(monkeypatch, tracker_calls):
    selected, pending = _select(monkeypatch, json.dumps({"selected": []}))
    assert selected == []
    assert pending == 0
    assert tracker_calls == []


def test_tracking_failure_never_fails_the_selection(monkeypatch):
    monkeypatch.setattr(
        injector, "_track_selector_usage", MagicMock(side_effect=RuntimeError("db down"))
    )
    fenced = "```json\n" + json.dumps({"selected": [{"category": "context", "title": "Slack"}]}) + "\n```"
    selected, pending = _select(monkeypatch, fenced, user_id="u-1", session_id=None)
    assert [e["title"] for e in selected] == ["Slack"]
    assert pending == 1
    assert injector._background_tracking == set()


def test_manifest_lines_and_fence_stripping():
    lines = injector._manifest_lines(_ENTRIES)
    assert lines[0] == '- [runbook] "Redis" | updated: 2024-01-01T00:00:00 | redis runbook'
    assert lines[1] == '- [context] "Slack" | updated: unknown | (no description)'
    assert json.loads(injector._strip_code_fence('```json\n{"selected": []}\n```')) == {"selected": []}
    assert injector._strip_code_fence('  {"selected": []}  ') == '{"selected": []}'
