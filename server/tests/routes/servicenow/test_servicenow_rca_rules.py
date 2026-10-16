"""Automatic ServiceNow RCA runs only for a saved filter (or the judge in Auto mode)."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

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

from routes.servicenow.rca_filters import (  # noqa: E402
    decide_webhook,
    extract_webhook_scope,
    match_kind,
    normalize_rca_rules,
    query_matches_incident,
    state_value,
)

SYS_ID = "a" * 32
RULES = [
    {"priorities": ["1", "2"]},
    {"assignmentGroups": ["Network"], "categories": ["Software"]},
]


def _incident(**fields) -> dict:
    body = {"sys_id": SYS_ID, "number": "INC0010001", "priority": "3", "state": "1",
            "assignment_group": "Service Desk", "category": "Inquiry / Help"}
    body.update(fields)
    return body


# ── normalisation ────────────────────────────────────────────────────


def test_rules_keep_choice_values_in_catalog_order_and_dedupe_names():
    rules, error = normalize_rca_rules([{
        "priorities": ["2 - High", 1, "2"],
        "assignmentGroups": [" Network ", "network", ""],
        "categories": None,
        "states": ["Resolved"] and ["6"],
        "query": "  cmdb_ci.name=payments  ",
    }])
    assert error is None
    assert rules == [{
        "assignmentGroups": ["Network"], "categories": [], "priorities": ["1", "2"],
        "states": ["6"], "query": "cmdb_ci.name=payments",
    }]


def test_identical_rules_collapse():
    rules, error = normalize_rca_rules([{"priorities": ["1"]}, {"priorities": ["1"]}])
    assert error is None
    assert len(rules) == 1


@pytest.mark.parametrize("raw, message", [
    ("nope", "must be a list"),
    ([{}], "at least one"),
    ([{"priorities": ["9"]}], "priorities must be one of"),
    ([{"states": ["4"]}], "states must be one of"),
    ([{"assignmentGroups": "Network"}], "must be a list"),
    ([{"assignmentGroups": ["x" * 81]}], "1–80"),
    ([{"query": "^priority=1"}], "no leading ^"),
    ([{"query": "priority=1^NQpriority=2"}], "no leading ^ and no ^NQ"),
    ([{"query": "priority=1\nx"}], "control characters"),
    ([{"query": "p" * 501}], "at most 500"),
    ([{"priorities": [True]}], "ServiceNow values"),
])
def test_invalid_rules_are_rejected(raw, message):
    rules, error = normalize_rca_rules(raw)
    assert rules is None
    assert message in error


def test_too_many_rules_rejected():
    rules, error = normalize_rca_rules([{"priorities": [str(1 + i % 5)], "query": f"q{i}"} for i in range(51)])
    assert rules is None and "At most 50" in error


# ── scope + matching ─────────────────────────────────────────────────


def test_scope_reads_priority_from_value_or_label_and_state_from_label():
    scope = extract_webhook_scope(_incident(priority="", priority_label="2 - High", state="", state_label="In Progress"))
    assert scope["priority"] == "2"
    assert scope["state"] == "2"
    assert scope["assignmentGroup"] == "Service Desk"


@pytest.mark.parametrize("payload, expected", [
    ({"state": "6"}, "6"), ({"state": "Resolved"}, "6"), ({"state_label": "cancelled"}, "8"), ({"state": "99"}, ""),
])
def test_state_value(payload, expected):
    assert state_value(payload) == expected


def test_priority_rule_matches_p1_and_misses_p3():
    assert match_kind(extract_webhook_scope(_incident(priority="1")), RULES) == "match"
    assert match_kind(extract_webhook_scope(_incident(priority="3")), RULES) == "miss"


def test_group_and_category_must_both_hit_and_are_case_insensitive():
    assert match_kind(extract_webhook_scope(_incident(assignment_group="NETWORK", category="software")), RULES) == "match"
    assert match_kind(extract_webhook_scope(_incident(assignment_group="Network", category="Hardware")), RULES) == "miss"


def test_query_rule_needs_confirmation_unless_a_plain_rule_also_hits():
    rules = [{"priorities": ["3"], "query": "impact=1"}]
    assert match_kind(extract_webhook_scope(_incident(priority="3")), rules) == "query"
    assert match_kind(extract_webhook_scope(_incident(priority="3")), rules + [{"priorities": ["3"]}]) == "match"
    assert match_kind(extract_webhook_scope(_incident(priority="4")), rules) == "miss"


def test_no_rules_means_nothing_triggers():
    assert match_kind(extract_webhook_scope(_incident(priority="1")), []) == "miss"
    assert match_kind(extract_webhook_scope(_incident(priority="1")), None) == "miss"


def test_decide_webhook_actions():
    assert decide_webhook(_incident(priority="1"), RULES)["action"] == "rca"
    assert decide_webhook(_incident(priority="4"), RULES) == {
        "action": "ignore", "reason": "filters", "scope": extract_webhook_scope(_incident(priority="4")),
    }
    decision = decide_webhook(_incident(priority="3"), [{"priorities": ["3"], "query": "impact=1"}])
    assert decision["action"] == "query" and decision["query"] == ["impact=1"]
    assert decide_webhook(_incident(priority="5"), [], "auto")["action"] == "judge"


# ── saved query confirmation ─────────────────────────────────────────


def test_query_confirmed_only_when_the_same_incident_comes_back():
    client = MagicMock()
    client.list_incidents.return_value = [{"sys_id": SYS_ID}]
    assert query_matches_incident(client, SYS_ID, ["impact=1"]) is True
    client.list_incidents.assert_called_once_with(query=f"sys_id={SYS_ID}^impact=1", limit=1, fields="sys_id")

    # An ^OR that breaks out and returns a different record is not a match.
    client.list_incidents.return_value = [{"sys_id": "b" * 32}]
    assert query_matches_incident(client, SYS_ID, ["impact=1^ORpriority=1"]) is False


def test_rejected_query_is_a_miss_but_other_errors_propagate():
    # Imported here, not at module level: tests/auth evicts routes.* from
    # sys.modules, and query_matches_incident binds the class at call time, so
    # a class bound at import time would no longer match its except clause.
    from routes.servicenow.servicenow_client import ServiceNowAPIError

    client = MagicMock()
    client.list_incidents.side_effect = ServiceNowAPIError("bad", 400)
    assert query_matches_incident(client, SYS_ID, ["impact=1"]) is False
    client.list_incidents.side_effect = ServiceNowAPIError("down", 503)
    with pytest.raises(ServiceNowAPIError):
        query_matches_incident(client, SYS_ID, ["impact=1"])
    assert query_matches_incident(client, "not-a-sys-id", ["impact=1"]) is False
