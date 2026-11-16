"""Revert replay task wiring and the shared review-agent runner.

``run_review_agent`` was extracted from the live PR review
(``_run_investigation_core``) so replay gets byte-for-byte the same agent
run and verdict parsing; these tests pin its contract for both callers.
Redis, Celery and the agent are mocked — no I/O.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tasks.change_gating as change_gating
import tasks.change_gating_replay as replay_task


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    module = ModuleType("utils.cache.redis_client")
    module.get_redis_client = lambda: fake
    monkeypatch.setitem(sys.modules, "utils.cache.redis_client", module)
    return fake


# ---------------------------------------------------------------------------
# Keys, enqueue, load
# ---------------------------------------------------------------------------


def test_keys_are_scoped_per_org():
    a = replay_task.replay_keys("org-a", "acme/api")
    b = replay_task.replay_keys("org-b", "acme/api")
    assert a["report"] != b["report"]
    assert a["lock"] != b["lock"]


def test_enqueue_takes_a_lock_so_a_second_click_does_not_queue_again(redis, monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(replay_task.replay_reverted_prs, "delay", delay)

    assert replay_task.enqueue_replay("u1", "org-a", 55, "acme/api") is True
    assert replay_task.enqueue_replay("u1", "org-a", 55, "acme/api") is False

    delay.assert_called_once_with("u1", "org-a", 55, "acme/api")
    assert replay_task.load_report("org-a", "acme/api") == {
        "repo_full_name": "acme/api", "status": "queued",
    }
    assert replay_task.load_report("org-b", "acme/api") is None


def test_a_failed_enqueue_does_not_leave_a_stuck_lock(redis, monkeypatch):
    monkeypatch.setattr(replay_task.replay_reverted_prs, "delay",
                        MagicMock(side_effect=ConnectionError("broker down")))
    with pytest.raises(ConnectionError):
        replay_task.enqueue_replay("u1", "org-a", 55, "acme/api")
    assert redis.store == {}


def test_task_stores_the_report_and_releases_the_lock(redis, monkeypatch):
    keys = replay_task.replay_keys("org-a", "acme/api")
    redis.set(keys["lock"], "queued")

    adapter = MagicMock()
    adapter_module = ModuleType("services.change_gating.github_adapter")
    adapter_module.GitHubPRAdapter = MagicMock(return_value=adapter)
    monkeypatch.setitem(sys.modules, "services.change_gating.github_adapter", adapter_module)

    def fake_run_replay(reader, repo, *, review, reverted_limit, control_limit, on_progress):
        assert reader is adapter
        on_progress({"status": "running", "results": []})
        return {"repo_full_name": repo, "status": "completed",
                "scores": {"reverted_flagged": 2, "reverted_reviewed": 3}}

    monkeypatch.setattr("services.change_gating.replay.run_replay", fake_run_replay)
    monkeypatch.setattr(replay_task, "_agent_reviewer", lambda user_id, repo: None)

    out = replay_task._replay("u1", "org-a", 55, "acme/api", 5, 5)

    assert out["status"] == "completed"
    assert json.loads(redis.get(keys["report"]))["scores"]["reverted_flagged"] == 2
    assert redis.get(keys["lock"]) is None
    adapter.close.assert_called_once()


def test_a_crashed_replay_is_reported_not_left_running(redis, monkeypatch):
    adapter_module = ModuleType("services.change_gating.github_adapter")
    adapter_module.GitHubPRAdapter = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "services.change_gating.github_adapter", adapter_module)

    def boom(*args, **kwargs):
        raise ConnectionError("github down")

    monkeypatch.setattr("services.change_gating.replay.run_replay", boom)
    monkeypatch.setattr(replay_task, "_agent_reviewer", lambda user_id, repo: None)

    out = replay_task._replay("u1", "org-a", 55, "acme/api", 5, 5)

    assert out["status"] == "failed"
    stored = replay_task.load_report("org-a", "acme/api")
    assert stored["status"] == "failed" and stored["error"] == "ConnectionError"


# ---------------------------------------------------------------------------
# run_review_agent (shared by live review and replay)
# ---------------------------------------------------------------------------


@pytest.fixture
def agent(monkeypatch):
    """Stub the background-chat module; returns a handle to steer the run."""
    state = SimpleNamespace(result={"status": "completed"}, final_text=None, kwargs=None)
    module = ModuleType("chat.background.task")
    module.create_background_chat_session = MagicMock(return_value="sess-1")

    def apply(kwargs):
        state.kwargs = kwargs
        return SimpleNamespace(result=state.result)

    module.run_background_chat = SimpleNamespace(apply=apply)
    monkeypatch.setitem(sys.modules, "chat.background.task", module)
    monkeypatch.setattr(change_gating, "_read_final_assistant_message",
                        lambda user_id, session_id: state.final_text)
    return state


def _run(**overrides):
    kwargs = dict(rail_text="t", title="PR Risk Review", trigger_metadata={"source": "change_gating"},
                  log_key="replay", log_ctx="repo=acme/api pr=1")
    kwargs.update(overrides)
    return change_gating.run_review_agent("u1", "PROMPT", **kwargs)


def test_runs_the_agent_read_only_and_parses_the_verdict(agent):
    agent.final_text = json.dumps({
        "verdict": "RISKY", "summary": "Pool too small.",
        "findings": [{"severity": "HIGH", "file_path": "config/db.yaml", "line": 1,
                      "title": "Pool exhausted", "explanation": "Timeouts at peak."}],
    })
    out = _run()
    assert out["status"] == "completed"
    assert out["session_id"] == "sess-1"
    assert out["verdict"]["verdict"] == "RISKY"
    assert agent.kwargs["mode"] == "ask"
    assert agent.kwargs["send_notifications"] is False
    assert agent.kwargs["initial_message"] == "PROMPT"


def test_risky_without_findings_is_demoted_to_safe(agent):
    agent.final_text = json.dumps({"verdict": "RISKY", "summary": "Hmm.", "findings": []})
    assert _run()["verdict"]["verdict"] == "SAFE"


@pytest.mark.parametrize("result,status", [
    ({"status": "failed"}, "agent_failed"),
    (RuntimeError("boom"), "agent_failed"),
    ({"status": "completed", "guardrail_blocked": True}, "guardrail_blocked"),
])
def test_agent_failures_carry_no_verdict(agent, result, status):
    agent.result = result
    out = _run()
    assert out["status"] == status
    assert "verdict" not in out


def test_unparseable_final_message(agent, monkeypatch):
    agent.final_text = "I could not finish the review."
    monkeypatch.setattr("services.change_gating.verdict.extract_verdict_with_llm", lambda text: None)
    assert _run()["status"] == "verdict_parse_failed"


# ---------------------------------------------------------------------------
# Toggle hook
# ---------------------------------------------------------------------------


def test_turning_incident_prevention_on_queues_one_replay(redis, monkeypatch):
    import routes.github.github_repo_selection as selection

    delay = MagicMock()
    monkeypatch.setattr(replay_task.replay_reverted_prs, "delay", delay)

    assert selection._start_replay("u1", "org-a", 55, "acme/api") is True
    # Toggling off and on again does not re-spend the agent runs.
    assert selection._start_replay("u1", "org-a", 55, "acme/api") is False
    delay.assert_called_once()


def test_a_replay_that_cannot_queue_never_breaks_the_toggle(monkeypatch):
    import routes.github.github_repo_selection as selection

    def broken(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(replay_task, "load_report", broken)
    assert selection._start_replay("u1", "org-a", 55, "acme/api") is False
