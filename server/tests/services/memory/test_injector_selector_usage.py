"""Memory selector usage tracking stays off the per-turn critical path
(services.memory.injector._select_relevant_memories_async).

The selector runs on every chat turn inside a prefetch bounded to a few seconds;
its ``llm_usage_tracking`` row (tiktoken count + DB insert with RLS set-up) must
be written in the background so a slow database can never time the prefetch out
and drop the memories the model already picked (review on #740). The write goes
to the process-owned usage writer thread, so it also survives ``asyncio.run()``
closing the loop at the end of a background RCA (review on #740, round 2).
"""

import asyncio
import json
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.memory import injector
from utils import usage_writer


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


class _TrackerSpy:
    """Stands in for the blocking tracker and records what the writer thread ran.

    ``gate`` starts open; a test closes it to hold the write back and then releases it,
    which orders events deterministically without any sleeping.
    """

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, messages, response, start_time, user_id, session_id):
        self.gate.wait(timeout=5)
        self.calls.append({"user_id": user_id, "session_id": session_id, "response": response})


@pytest.fixture
def tracker(monkeypatch):
    spy = _TrackerSpy()
    monkeypatch.setattr(injector, "_track_selector_usage", spy)
    return spy


def _use_model(monkeypatch, content):
    monkeypatch.setattr(injector, "get_cached_chat_model", lambda *a, **k: _FakeLLM(content))


def _select(monkeypatch, content, **kwargs):
    """Run the selector on a fake model, then wait for the usage writer to go idle."""
    _use_model(monkeypatch, content)
    selected = asyncio.run(
        injector._select_relevant_memories_async("why is redis slow", _ENTRIES, **kwargs)
    )
    assert usage_writer.drain(timeout=5) is True
    return selected


def test_selection_is_returned_before_the_usage_row_is_written(monkeypatch, tracker):
    content = json.dumps({"selected": [{"category": "runbook", "title": "Redis"}]})
    _use_model(monkeypatch, content)
    tracker.gate.clear()  # the write cannot finish until the selection has come back

    try:
        selected = asyncio.run(
            injector._select_relevant_memories_async(
                "why is redis slow", _ENTRIES, user_id="u-1", session_id="s-1"
            )
        )
        recorded_before_release = len(tracker.calls)
    finally:
        tracker.gate.set()
    assert usage_writer.drain(timeout=5) is True

    assert [e["title"] for e in selected] == ["Redis"]
    assert recorded_before_release == 0
    assert len(tracker.calls) == 1
    assert tracker.calls[0]["user_id"] == "u-1"
    assert tracker.calls[0]["session_id"] == "s-1"
    assert tracker.calls[0]["response"].content == content


def test_usage_row_survives_the_event_loop_closing(monkeypatch, tracker):
    """Review on #740 (round 2): ``asyncio.run()`` cancels what is still pending when the
    workflow returns (and otherwise holds the return until the loop's default executor
    has drained), so a fire-and-forget task could lose the ``memory_selector`` row or
    hold the run hostage to the database. The write is held back until ``asyncio.run``
    has returned: it can only complete on the usage writer thread, after the loop is
    gone, and the run did not wait for it."""
    content = json.dumps({"selected": []})
    _use_model(monkeypatch, content)
    tracker.gate.clear()

    try:
        selected = asyncio.run(
            injector._select_relevant_memories_async(
                "why is redis slow", _ENTRIES, user_id="u-2", session_id=None
            )
        )
        written_inside_the_loop = len(tracker.calls)
    finally:
        tracker.gate.set()  # the loop is closed: let the writer finish
    assert usage_writer.drain(timeout=5) is True

    assert selected == []
    assert written_inside_the_loop == 0
    assert len(tracker.calls) == 1
    assert tracker.calls[0]["user_id"] == "u-2"
    assert tracker.calls[0]["session_id"] is None


def test_nothing_is_tracked_without_a_user_id(monkeypatch, tracker):
    submitted = []
    monkeypatch.setattr(injector.usage_writer, "submit", lambda *args, **kwargs: submitted.append(args))

    selected = _select(monkeypatch, json.dumps({"selected": []}))

    assert selected == []
    assert submitted == []
    assert tracker.calls == []


def test_tracking_failure_never_fails_the_selection(monkeypatch, caplog):
    failing = MagicMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(injector, "_track_selector_usage", failing)
    fenced = "```json\n" + json.dumps({"selected": [{"category": "context", "title": "Slack"}]}) + "\n```"

    with caplog.at_level("DEBUG", logger=injector.logger.name):
        selected = _select(monkeypatch, fenced, user_id="u-1", session_id=None)

    assert [e["title"] for e in selected] == ["Slack"]
    failing.assert_called_once()
    # The failure is logged (debug) by the injector itself, as before.
    assert any("Could not record selector usage" in r.getMessage() for r in caplog.records)


def test_manifest_lines_and_fence_stripping():
    lines = injector._manifest_lines(_ENTRIES)
    assert lines[0] == '- [runbook] "Redis" | updated: 2024-01-01T00:00:00 | redis runbook'
    assert lines[1] == '- [context] "Slack" | updated: unknown | (no description)'
    assert json.loads(injector._strip_code_fence('```json\n{"selected": []}\n```')) == {"selected": []}
    assert injector._strip_code_fence('  {"selected": []}  ') == '{"selected": []}'
