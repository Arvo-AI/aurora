"""Slack "Thinking…" placeholder gets live, clearly-tentative progress edits.

Pins the contract that matters to a reader in Slack: the note is labelled as
in-progress, shows elapsed time / running tool / latest sentence, and stops
editing before the final answer is written to the same message.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def bg():
    """chat.background imports task.py, which builds a Redis client at import; stub it."""
    import utils.cache.redis_client as redis_client

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redis_client, "get_redis_client", MagicMock)
        mp.delitem(sys.modules, "chat.background.task", raising=False)
        import chat.background.task as task
        from chat.background import slack_progress as sp
        from chat.background.background_websocket import BackgroundWebSocket

        yield SimpleNamespace(task=task, sp=sp, BackgroundWebSocket=BackgroundWebSocket)


# ---------------------------------------------------------------------------
# Pure formatting
# ---------------------------------------------------------------------------

def test_format_elapsed(bg):
    assert bg.sp.format_elapsed(5) == "5s"
    assert bg.sp.format_elapsed(65) == "1m 05s"
    assert bg.sp.format_elapsed(3725) == "1h 02m"


def test_latest_thought_drops_unfinished_fragment_and_markdown(bg):
    buffer = "## Plan\n\nBatch **1** worked. Next is `batch 2` (22 clusters). Now I will che"
    assert bg.sp.latest_thought(buffer) == "Batch 1 worked. Next is batch 2 (22 clusters)."


def test_latest_thought_keeps_only_fragment_when_nothing_else(bg):
    assert bg.sp.latest_thought("Checking the first cluster") == "Checking the first cluster"


def test_latest_thought_caps_length_with_most_recent_sentences(bg):
    buffer = " ".join(f"Sentence number {i} is here." for i in range(40))
    thought = bg.sp.latest_thought(buffer, max_chars=80)
    assert len(thought) <= 80
    assert thought.endswith("Sentence number 39 is here.")


def test_describe_tool_shortens_command_and_escapes_backticks(bg):
    desc = bg.sp.describe_tool("cloud_exec", {"command": "kubectl logs `pod` -n ns " + "x" * 200})
    assert desc.startswith("`cloud_exec` — `kubectl logs 'pod' -n ns")
    assert desc.endswith("…`")
    assert len(desc) < 130


def test_describe_tool_without_command_is_just_the_name(bg):
    assert bg.sp.describe_tool("search_memory", {"query_vector": [1, 2]}) == "`search_memory`"


def test_render_progress_note_is_labelled_as_not_the_answer(bg):
    note = bg.sp.render_progress_note(
        elapsed_seconds=380,
        completed_tool_calls=12,
        running_tool="`cloud_exec` — `kubectl get pods`",
        thought="Batch 1 worked.",
    )
    assert "Still working on this" in note
    assert "6m 20s elapsed" in note
    assert "12 tool calls so far" in note
    assert "not the answer" in note
    assert "replaced with the full response" in note
    assert "*Currently running:* `cloud_exec` — `kubectl get pods`" in note
    assert "> Batch 1 worked." in note
    assert "(may change)" in note


def test_render_progress_note_omits_empty_sections(bg):
    note = bg.sp.render_progress_note(elapsed_seconds=12, completed_tool_calls=0, running_tool=None, thought="")
    assert "Currently running" not in note
    assert "Latest thought" not in note
    assert "tool call" not in note


def test_progress_interval_env_and_floor(bg, monkeypatch):
    monkeypatch.delenv("SLACK_PROGRESS_UPDATE_INTERVAL_SECONDS", raising=False)
    assert bg.sp.progress_interval_seconds() == bg.sp.DEFAULT_INTERVAL_SECONDS
    monkeypatch.setenv("SLACK_PROGRESS_UPDATE_INTERVAL_SECONDS", "2")
    assert bg.sp.progress_interval_seconds() == 10.0
    monkeypatch.setenv("SLACK_PROGRESS_UPDATE_INTERVAL_SECONDS", "garbage")
    assert bg.sp.progress_interval_seconds() == bg.sp.DEFAULT_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Reporter wiring
# ---------------------------------------------------------------------------

def _capture(running=(), completed=0):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        lock=threading.Lock(),
        current_tool_calls={
            f"id{i}": {"tool_name": name, "input": inp, "start_time": now}
            for i, (name, inp) in enumerate(running)
        },
        tool_history=[{"completed_at": "x"} for _ in range(completed)] + [{"completed_at": None}],
    )


def _reporter(bg, capture=None, interval=0.01):
    wf = SimpleNamespace(agent=SimpleNamespace(tool_capture_instance=capture))
    return bg.sp.SlackProgressReporter(
        user_id="uid-test", channel="C000", message_ts="1.0", workflow=wf, interval_seconds=interval
    )


def test_observe_collects_only_streamed_text_frames(bg):
    r = _reporter(bg)
    r.observe(json.dumps({"type": "message", "data": {"text": "First sentence. ", "is_chunk": True}}))
    r.observe({"type": "message", "data": {"text": "Second one."}})
    r.observe({"type": "tool_call", "data": {"tool_name": "cloud_exec"}})
    r.observe({"type": "usage_update", "data": {"output_chunks": 3}})
    r.observe("not json")
    assert bg.sp.latest_thought(r._text_buffer) == "First sentence. Second one."


def test_snapshot_reads_running_tool_and_history_from_capture(bg):
    cap = _capture(running=[("cloud_exec", {"command": "kubectl get pods -A"})], completed=4)
    r = _reporter(bg, cap)
    r.observe({"type": "message", "data": {"text": "Pulling pod list now."}})
    note = r.snapshot()
    assert "4 tool calls so far" in note
    assert "*Currently running:* `cloud_exec` — `kubectl get pods -A`" in note
    assert "> Pulling pod list now." in note


def test_snapshot_redacts_secrets_in_running_command(bg):
    token = "AKIA" + "ABCDEFGHIJKLMNOP"  # AWS access key id shape
    cap = _capture(running=[("cloud_exec", {"command": f"aws sts get-caller-identity --key {token}"})])
    note = _reporter(bg, cap).snapshot()
    assert token not in note
    assert "*Currently running:* `cloud_exec`" in note


def test_snapshot_survives_missing_capture(bg):
    note = _reporter(bg, capture=None).snapshot()
    assert "Still working on this" in note


def test_run_edits_placeholder_until_stopped_and_never_after(bg):
    r = _reporter(bg, _capture())
    sent = []

    class _Client:
        def update_message(self, channel, ts, text):
            sent.append((channel, ts, text))

    r._client = _Client()

    async def scenario():
        task = asyncio.create_task(r.run())
        await asyncio.sleep(0.06)
        r.stop()
        await task
        count = len(sent)
        await asyncio.sleep(0.03)
        return count

    count = asyncio.run(scenario())
    assert count >= 2
    assert len(sent) == count, "no edits may land after stop() returns"
    assert all(c == "C000" and ts == "1.0" for c, ts, _ in sent)
    assert "not the answer" in sent[-1][2]
    assert r.updates_sent == count


def test_run_gives_up_after_consecutive_failures(bg):
    r = _reporter(bg)
    calls = []

    class _Client:
        def update_message(self, **kw):
            calls.append(kw)
            raise RuntimeError("message_not_found")

    r._client = _Client()
    asyncio.run(asyncio.wait_for(r.run(), timeout=2))
    assert len(calls) == bg.sp._MAX_CONSECUTIVE_FAILURES
    assert r.updates_sent == 0


def test_stop_before_run_returns_immediately(bg):
    r = _reporter(bg)
    r.stop()
    asyncio.run(asyncio.wait_for(r.run(), timeout=1))


def test_background_websocket_forwards_frames_and_swallows_observer_errors(bg):
    seen = []

    def observer(frame):
        seen.append(frame)
        raise ValueError("boom")

    ws = bg.BackgroundWebSocket(on_message=observer)
    asyncio.run(ws.send('{"type":"message"}'))
    asyncio.run(bg.BackgroundWebSocket().send("ignored"))
    assert seen == ['{"type":"message"}']


# ---------------------------------------------------------------------------
# Task-side gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "meta, expect",
    [
        ({"source": "slack", "channel": "C1", "thinking_message_ts": "1.2"}, True),
        ({"source": "slack_button", "channel": "C1", "thinking_message_ts": "1.2"}, True),
        ({"source": "slack", "channel": "C1"}, False),
        ({"source": "google_chat", "channel": "C1", "thinking_message_ts": "1.2"}, False),
        ({"source": "grafana"}, False),
        (None, False),
    ],
)
def test_build_reporter_only_for_slack_turns_with_placeholder(bg, monkeypatch, meta, expect):
    monkeypatch.delenv("SLACK_PROGRESS_UPDATES_ENABLED", raising=False)
    reporter = bg.task._build_slack_progress_reporter("uid-test", meta, wf=None)
    assert (reporter is not None) is expect


def test_build_reporter_respects_kill_switch(bg, monkeypatch):
    monkeypatch.setenv("SLACK_PROGRESS_UPDATES_ENABLED", "false")
    meta = {"source": "slack", "channel": "C1", "thinking_message_ts": "1.2"}
    assert bg.task._build_slack_progress_reporter("uid-test", meta, wf=None) is None
