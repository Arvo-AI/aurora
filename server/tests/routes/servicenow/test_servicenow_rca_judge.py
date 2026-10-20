"""Auto mode: the judge decides which ServiceNow incidents get an RCA."""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_import_stubs() -> None:
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)
    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                return fn
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    sys.modules.setdefault("celery_config", celery_cfg)


_install_import_stubs()

from routes.servicenow import rca_judge  # noqa: E402
from routes.servicenow.rca_judge import (  # noqa: E402
    JudgeUnavailable,
    JudgeVerdict,
    incident_fingerprint,
    incident_for_judge,
    judge_and_route,
)

SYS_ID = "c" * 32


def _payload(short_description="Checkout returns 500 for customers", **fields) -> dict:
    body = {
        "sys_id": SYS_ID, "number": "INC0010007", "priority": "2", "priority_label": "2 - High",
        "state": "1", "category": "Software", "assignment_group": "Platform",
        "cmdb_ci": "checkout-api", "caller": "Support Agent",
        "short_description": short_description, "description": "Customers see an error at payment.",
    }
    body.update(fields)
    return body


def test_incident_for_judge_keeps_the_deciding_fields_and_truncates():
    incident = incident_for_judge(_payload(short_description="x" * 1000))
    assert incident["number"] == "INC0010007"
    assert incident["priority"] == "2"
    assert incident["state"] == "New"
    assert incident["configuration_item"] == "checkout-api"
    assert len(incident["short_description"]) == 300


def test_fingerprint_ignores_state_moves_but_not_content_changes():
    base = incident_fingerprint(incident_for_judge(_payload()))
    moved = incident_fingerprint(incident_for_judge(_payload(state="2", assigned_to="Someone")))
    reworded = incident_fingerprint(incident_for_judge(_payload(short_description="Order a new laptop")))
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
         patch.object(rca_judge, "_servicenow_policy", return_value="Investigate outages."), \
         patch.object(rca_judge, "judge_incident") as judge:
        yield {
            "run": lambda payload: judge_and_route(payload, "uid-1", start_rca=start_rca),
            "start_rca": start_rca,
            "judge": judge,
            "redis": redis_client,
        }


def test_investigate_verdict_starts_an_rca(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="outage")
    assert judge_env["run"](_payload()) == {"status": "judged", "investigate": True}
    judge_env["start_rca"].assert_called_once()


def test_a_start_that_ran_no_rca_is_not_remembered_as_investigated(judge_env):
    """start_rca returning False (the incident arrived already resolved) keeps only the fingerprint."""
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="outage")
    judge_env["start_rca"].return_value = False
    assert judge_env["run"](_payload()) == {"status": "judged", "investigate": True}
    judge_env["start_rca"].assert_called_once()
    remembered = judge_env["redis"].set.call_args.args[1]
    assert remembered != "investigated"
    assert remembered == incident_fingerprint(incident_for_judge(_payload()))


def test_skip_verdict_starts_nothing(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="service request")
    judge_env["run"](_payload("Order a new laptop"))
    judge_env["start_rca"].assert_not_called()


def test_an_edit_that_changes_nothing_is_not_judged_again(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="service request")
    judge_env["run"](_payload())
    assert judge_env["run"](_payload(state="2")) == {"status": "unchanged"}
    assert judge_env["judge"].call_count == 1


def test_a_failed_rca_handoff_is_judged_again(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="outage")
    judge_env["start_rca"].side_effect = ConnectionError("db down")
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


def test_an_investigated_incident_is_not_rejudged_when_edited(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="outage")
    judge_env["run"](_payload())
    assert judge_env["run"](_payload("Checkout still returns 500 after the rollback")) == {"status": "unchanged"}
    assert judge_env["judge"].call_count == 1
    judge_env["start_rca"].assert_called_once()


def test_a_skipped_incident_is_rejudged_when_its_content_changes(judge_env):
    judge_env["judge"].return_value = JudgeVerdict(investigate=False, reason="request")
    judge_env["run"](_payload("Order a new laptop"))
    judge_env["judge"].return_value = JudgeVerdict(investigate=True, reason="now an outage")
    assert judge_env["run"](_payload("Laptop fleet cannot reach the VPN")) == {"status": "judged", "investigate": True}
    assert judge_env["judge"].call_count == 2


def test_policy_read_failure_is_retried_not_defaulted():
    with patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org-test"), \
         patch.object(rca_judge, "_redis", return_value=None), \
         patch("utils.hooks.get_hook", return_value=lambda org_id, user_id: (True, "")), \
         patch("services.memory.queries.get_memory_content", side_effect=RuntimeError("db down")):
        with pytest.raises(JudgeUnavailable):
            judge_and_route(_payload(), "uid-1", start_rca=MagicMock())
