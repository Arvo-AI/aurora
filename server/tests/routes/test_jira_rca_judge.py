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
    with pytest.raises(ConnectionError):
        judge_env["run"](_payload())
    judge_env["start_rca"].side_effect = None
    assert judge_env["run"](_payload())["status"] == "judged"


def test_hourly_limit_stops_the_judge(judge_env):
    judge_env["redis"].incr.return_value = 10_000
    assert judge_env["run"](_payload()) == {"status": "rate_limited"}
    judge_env["judge"].assert_not_called()


def test_no_verdict_asks_for_a_retry_and_remembers_nothing(judge_env):
    judge_env["judge"].return_value = None
    with pytest.raises(JudgeUnavailable):
        judge_env["run"](_payload())
    judge_env["start_rca"].assert_not_called()
    judge_env["redis"].set.assert_not_called()
