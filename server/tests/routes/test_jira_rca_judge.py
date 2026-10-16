"""Auto mode: the judge decides which Jira tickets get an RCA."""

from unittest.mock import MagicMock, patch

import pytest

from routes.jira import rca_judge
from routes.jira.rca_judge import (
    JudgeUnavailable,
    JudgeVerdict,
    judge_and_route,
    ticket_fingerprint,
    ticket_for_judge,
)


def _payload(summary="Checkout returns 500 for customers", **fields) -> dict:
    body = {
        "project": {"key": "OPS"},
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "status": {"name": "To Do"},
        "summary": summary,
        "description": "Customers see an error at payment.",
        "reporter": {"displayName": "Support Agent"},
    }
    body.update(fields)
    return {"webhookEvent": "jira:issue_created", "issue": {"key": "OPS-7", "fields": body}}


def test_ticket_for_judge_keeps_the_deciding_fields_and_truncates():
    ticket = ticket_for_judge(_payload(summary="x" * 1000)["issue"])
    assert ticket["key"] == "OPS-7"
    assert ticket["type"] == "Bug"
    assert ticket["reporter"] == "Support Agent"
    assert len(ticket["summary"]) == 300


def test_fingerprint_ignores_status_moves_but_not_content_changes():
    base = ticket_fingerprint(ticket_for_judge(_payload()["issue"]))
    moved = ticket_fingerprint(ticket_for_judge(_payload(status={"name": "Done"})["issue"]))
    reworded = ticket_fingerprint(ticket_for_judge(_payload(summary="Send the weekly email")["issue"]))
    assert base == moved
    assert base != reworded


@pytest.fixture
def judge_env():
    """Run judge_and_route with the LLM, Redis, and org lookups stubbed."""
    redis_store = {}
    redis_client = MagicMock()
    redis_client.get.side_effect = redis_store.get
    redis_client.set.side_effect = lambda key, value, ex=None: redis_store.__setitem__(key, value)
    redis_client.incr.return_value = 1
    start_rca = MagicMock()

    with patch.object(rca_judge, "_redis", return_value=redis_client), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org-test"), \
         patch("utils.hooks.get_hook", return_value=lambda org_id, user_id: (True, "")), \
         patch.object(rca_judge, "_jira_policy", return_value="Investigate customer bugs."), \
         patch.object(rca_judge, "judge_ticket") as judge:
        yield {
            "run": lambda payload: judge_and_route(payload, "uid-1", start_rca=start_rca),
            "start_rca": start_rca,
            "judge": judge,
            "redis": redis_client,
        }


def test_investigate_verdict_starts_an_rca(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="customer bug")
    assert judge_env["run"](_payload()) == {"status": "judged", "investigate": True}
    judge_env["start_rca"].assert_called_once()


def test_skip_verdict_starts_nothing(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="admin task")
    judge_env["run"](_payload("Send the email"))
    judge_env["start_rca"].assert_not_called()


def test_an_edit_that_changes_nothing_is_not_judged_again(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="admin task")
    judge_env["run"](_payload())
    assert judge_env["run"](_payload(status={"name": "In Progress"})) == {"status": "unchanged"}
    assert judge_env["judge"].call_count == 1


def test_a_failed_rca_handoff_is_judged_again(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="customer bug")
    judge_env["start_rca"].side_effect = ConnectionError("broker down")
    payload = _payload()
    with pytest.raises(ConnectionError):
        judge_env["run"](payload)
    judge_env["start_rca"].side_effect = None
    assert judge_env["run"](_payload())["status"] == "judged"


def test_hourly_limit_stops_the_judge(judge_env):
    judge_env["redis"].incr.return_value = 10_000
    assert judge_env["run"](_payload()) == {"status": "rate_limited"}
    judge_env["judge"].assert_not_called()


def test_no_verdict_asks_for_a_retry_and_remembers_nothing(judge_env):
    judge_env["judge"].return_value = None
    payload = _payload()
    with pytest.raises(JudgeUnavailable):
        judge_env["run"](payload)
    judge_env["start_rca"].assert_not_called()
    judge_env["redis"].set.assert_not_called()


def test_an_investigated_ticket_is_not_rejudged_when_edited(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="customer bug")
    judge_env["run"](_payload())
    edited = _payload(summary="Checkout still returns 500 after the rollback")
    assert judge_env["run"](edited) == {"status": "unchanged"}
    assert judge_env["judge"].call_count == 1
    judge_env["start_rca"].assert_called_once()


def test_a_skipped_ticket_is_rejudged_when_its_content_changes(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="admin task")
    judge_env["run"](_payload("Send the weekly email"))
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="now a customer bug")
    result = judge_env["run"](_payload("Checkout returns 500 for customers"))
    assert result == {"status": "judged", "investigate": True}
    assert judge_env["judge"].call_count == 2


def test_keyless_tickets_are_not_deduped_together(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="skip")
    first = _payload()
    first["issue"]["key"] = ""
    second = _payload(summary="A different ticket with no key")
    second["issue"]["key"] = ""
    assert judge_env["run"](first)["status"] == "judged"
    assert judge_env["run"](second)["status"] == "judged"
    assert judge_env["judge"].call_count == 2
    judge_env["redis"].set.assert_not_called()


def test_a_policy_read_failure_retries_instead_of_using_defaults():
    with patch("services.memory.queries.get_memory_content", side_effect=RuntimeError("db down")), \
         patch("services.memory.jira_memory.seed_jira_memory") as seed:
        with pytest.raises(JudgeUnavailable, match="policy unavailable"):
            rca_judge._jira_policy("uid-1")
    seed.assert_not_called()


def test_a_missing_policy_is_seeded_and_judged_with_the_defaults():
    from services.memory.jira_memory import JIRA_MEMORY_DEFAULT_CONTENT

    with patch("services.memory.queries.get_memory_content", return_value=None), \
         patch("services.memory.jira_memory.seed_jira_memory") as seed:
        assert rca_judge._jira_policy("uid-1") == JIRA_MEMORY_DEFAULT_CONTENT
    seed.assert_called_once_with("uid-1")


def test_judge_ticket_uses_the_judge_model(monkeypatch):
    from chat.backend.agent.llm import ModelConfig

    monkeypatch.setattr(ModelConfig, "JIRA_RCA_JUDGE_MODEL", "test/judge-model")
    llm = MagicMock()
    structured = MagicMock()
    structured.invoke.return_value = {
        "parsed": JudgeVerdict(investigate=True, reason="ok"),
        "raw": None,
    }
    llm.with_structured_output.return_value = structured
    with patch("chat.backend.agent.providers.create_chat_model", return_value=llm) as create, \
         patch("chat.backend.agent.utils.llm_usage_tracker.LLMUsageTracker.track_llm_call"):
        rca_judge.judge_ticket("uid-1", "Investigate customer bugs.", {"key": "OPS-1"})
    assert create.call_args.kwargs["model"] == "test/judge-model"


def test_judge_task_retries_any_failure(monkeypatch):
    """Import the task with Celery stubbed. The real app pulls Redis in at import."""
    import sys
    from types import ModuleType

    celery_cfg = ModuleType("celery_config")
    captured: dict = {}

    class _Self:
        def retry(self, exc):
            captured["exc"] = exc
            raise RuntimeError("retried")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                def _run(*args, **kwargs):
                    return fn(_Self(), *args, **kwargs)
                return _run
            if dargs and callable(dargs[0]):
                return _decorator(dargs[0])
            return _decorator

    celery_cfg.celery_app = _App()
    background = ModuleType("chat.background")
    background.__path__ = []  # type: ignore[attr-defined]
    prompt_builder = ModuleType("chat.background.rca_prompt_builder")
    prompt_builder.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "celery_config", celery_cfg)
    monkeypatch.setitem(sys.modules, "chat.background", background)
    monkeypatch.setitem(sys.modules, "chat.background.rca_prompt_builder", prompt_builder)
    sys.modules.pop("routes.jira.tasks", None)
    try:
        from routes.jira import tasks
        with patch("routes.jira.rca_judge.judge_and_route", side_effect=ConnectionError("broker down")):
            with pytest.raises(RuntimeError, match="retried"):
                tasks.judge_jira_ticket(_payload(), "uid-1")
    finally:
        sys.modules.pop("routes.jira.tasks", None)
    assert isinstance(captured["exc"], ConnectionError)
