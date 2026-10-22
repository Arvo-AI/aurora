"""Retry flow of the post-RCA summary task (chat.background.summarization).

A retry may reuse the summary a previous attempt stored only when THAT attempt
stored it (the flag travels through ``self.retry``). ``self.request.retries``
alone cannot tell a stored-by-us summary from the previous report of an incident
that was already ``complete`` when a regeneration was enqueued (review on #740):
reusing the old report there would skip the model call and notify stale data.

The task body is exercised with an explicit fake ``self``: ``chat.background``
imports ``task.py``, which builds a Redis client at import, so that client is
stubbed exactly like ``tests/tasks/test_slack_progress.py`` does.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def summarization():
    import utils.cache.redis_client as redis_client

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redis_client, "get_redis_client", MagicMock)
        mp.delitem(sys.modules, "chat.background.task", raising=False)
        import chat.background.summarization as mod

        yield mod


class _Retry(Exception):
    """Stands in for the exception ``Task.retry`` raises."""


def _fake_task(retries: int = 0, request_kwargs: dict | None = None):
    task = SimpleNamespace(
        request=SimpleNamespace(retries=retries, kwargs=request_kwargs or {}),
        max_retries=2,
    )
    task.retry = MagicMock(side_effect=_Retry("retry"))
    return task


_KWARGS = {"incident_id": "inc-1", "user_id": "u-1", "session_id": "s-1", "announce_completion": True}


@pytest.fixture
def harness(summarization, monkeypatch):
    """Stub everything around the summary itself; returns the spies."""
    import utils.auth.stateless_auth as stateless_auth
    import utils.hooks as hooks

    monkeypatch.setattr(hooks, "get_hook", lambda name: (lambda org, user: (True, "")))
    monkeypatch.setattr(stateless_auth, "get_org_id_for_user", lambda user_id: "org-1")

    for name in (
        "services.correlation.recurrence_agent",
        "services.memory.incident_index",
        "utils.notifications.dispatcher",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["services.correlation.recurrence_agent"].run_recurrence_check = MagicMock(return_value=None)
    index = sys.modules["services.memory.incident_index"]
    index.append_incident_line = MagicMock(return_value=True)
    index.generate_root_cause_synopsis = MagicMock(return_value="synopsis")
    index.record_recurrence = MagicMock(return_value=True)
    notify = MagicMock(name="notify_investigation_completed")
    sys.modules["utils.notifications.dispatcher"].notify_investigation_completed = notify

    basics = {"source_type": "grafana", "alert_title": "p99", "severity": "high", "service": "checkout",
              "triggered_at": "2026-10-22T10:00:00"}
    spies = SimpleNamespace(
        basics=MagicMock(return_value=basics),
        stored=MagicMock(return_value="OLD REPORT (already complete before this run)"),
        generate=MagicMock(return_value=("NEW REPORT", [])),
        update=MagicMock(),
        notify=notify,
    )
    monkeypatch.setattr(summarization, "_fetch_incident_basics", spies.basics)
    monkeypatch.setattr(summarization, "_fetch_stored_rca_summary", spies.stored)
    monkeypatch.setattr(summarization, "_generate_chat_summary", spies.generate)
    monkeypatch.setattr(summarization, "_update_incident_summary", spies.update)
    return spies


def _run(summarization, task, **overrides):
    body = summarization.generate_incident_summary_from_chat.run.__func__
    return body(task, **{**_KWARGS, **overrides})


def test_regeneration_retry_calls_the_model_even_though_a_summary_is_stored(summarization, harness):
    """Retry of a regeneration (incident already ``complete``, model call failed the
    first time): the stored report is the OLD one and must not be reused."""
    task = _fake_task(retries=1, request_kwargs=_KWARGS)

    result = _run(summarization, task)

    harness.generate.assert_called_once()
    harness.stored.assert_not_called()
    harness.update.assert_called_once_with("inc-1", "NEW REPORT", user_id="u-1")
    harness.notify.assert_called_once()
    assert result["status"] == "completed"


def test_retry_after_a_post_summary_failure_carries_the_reuse_flag(summarization, harness):
    """The summary was stored by this attempt and a later step hit a transient error:
    the retry is told it may reuse the stored summary."""
    harness.notify.side_effect = TimeoutError("notification backend slow")
    task = _fake_task(retries=0, request_kwargs=_KWARGS)

    with pytest.raises(_Retry):
        _run(summarization, task)

    harness.update.assert_called_once()
    retry_kwargs = task.retry.call_args.kwargs
    assert isinstance(retry_kwargs["exc"], TimeoutError)
    assert retry_kwargs["kwargs"] == {**_KWARGS, "reuse_stored_summary": True}


def test_retry_with_the_flag_reuses_the_stored_summary_and_skips_the_model(summarization, harness):
    task = _fake_task(retries=1, request_kwargs={**_KWARGS, "reuse_stored_summary": True})

    result = _run(summarization, task, reuse_stored_summary=True)

    harness.stored.assert_called_once_with("inc-1", "u-1")
    harness.generate.assert_not_called()
    harness.update.assert_not_called()
    harness.notify.assert_called_once()
    assert result["status"] == "completed"


def test_retry_after_a_failed_model_call_does_not_carry_the_flag(summarization, harness):
    harness.generate.side_effect = TimeoutError("model timed out")
    task = _fake_task(retries=0, request_kwargs=_KWARGS)

    with pytest.raises(_Retry):
        _run(summarization, task)

    harness.update.assert_not_called()
    assert task.retry.call_args.kwargs["kwargs"] == {**_KWARGS, "reuse_stored_summary": False}


def test_non_retryable_failure_before_the_summary_marks_the_incident_failed(summarization, harness):
    harness.generate.side_effect = ValueError("bad model id")
    task = _fake_task(retries=0, request_kwargs=_KWARGS)

    result = _run(summarization, task)

    task.retry.assert_not_called()
    assert result["status"] == "failed"
    harness.update.assert_called_once_with(
        "inc-1", "Summary generation failed. View investigation chat for details.",
        status="error", user_id="u-1",
    )
