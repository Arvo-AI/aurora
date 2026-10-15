"""Automatic Jira RCA runs only for a saved filter."""

from routes.jira.rca_filters import (
    decide_webhook,
    extract_webhook_scope,
    jql_matches_issue,
    match_kind,
    normalize_rca_rules,
    rca_rule_matches,
)


RULES = [
    {"projectKey": "OPS", "issueTypes": ["Bug", "Incident"]},
    {"projectKey": "RD", "issueTypes": ["Defect"]},
]

_OPEN = {
    "priorities": [],
    "labels": [],
    "components": [],
    "statuses": [],
    "jql": "",
}


def _issue(project: str, issue_type: str, key: str = "", **fields) -> dict:
    body = {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
    }
    body.update(fields)
    return {"key": key or f"{project}-1", "fields": body}


def test_matching_project_and_type_triggers():
    scope = extract_webhook_scope(_issue("OPS", "Bug"))
    assert rca_rule_matches(scope, RULES) is True


def test_match_is_case_insensitive():
    scope = extract_webhook_scope(_issue("ops", "incident"))
    assert rca_rule_matches(scope, RULES) is True


def test_same_project_wrong_type_does_not_trigger():
    scope = extract_webhook_scope(_issue("OPS", "Story"))
    assert rca_rule_matches(scope, RULES) is False


def test_other_project_does_not_trigger():
    scope = extract_webhook_scope(_issue("OTHER", "Bug"))
    assert rca_rule_matches(scope, RULES) is False


def test_no_rules_means_nothing_triggers():
    scope = extract_webhook_scope(_issue("OPS", "Bug"))
    assert rca_rule_matches(scope, []) is False
    assert rca_rule_matches(scope, None) is False


def test_issue_key_supplies_project_when_field_is_missing():
    issue = {"key": "OPS-42", "fields": {"issuetype": {"name": "Bug"}}}
    scope = extract_webhook_scope(issue)
    assert (scope["projectKey"], scope["issueType"]) == ("OPS", "Bug")
    assert rca_rule_matches(scope, RULES) is True


def test_missing_type_does_not_trigger():
    scope = extract_webhook_scope({"key": "OPS-1", "fields": {"project": {"key": "OPS"}}})
    assert rca_rule_matches(scope, RULES) is False


def test_priority_label_component_and_status_narrow_the_match():
    rules = [{
        "projectKey": "OPS",
        "issueTypes": ["Bug"],
        "priorities": ["High"],
        "labels": ["prod"],
        "components": ["api"],
        "statuses": ["In Progress"],
    }]
    hit = extract_webhook_scope(_issue(
        "OPS", "Bug",
        priority={"name": "high"},
        labels=["team", "prod"],
        components=[{"name": "api"}],
        status={"name": "In Progress"},
    ))
    miss = extract_webhook_scope(_issue(
        "OPS", "Bug",
        priority={"name": "Low"},
        labels=["prod"],
        components=[{"name": "api"}],
        status={"name": "In Progress"},
    ))
    assert rca_rule_matches(hit, rules) is True
    assert rca_rule_matches(miss, rules) is False


def test_empty_optional_fields_do_not_narrow():
    scope = extract_webhook_scope(_issue("OPS", "Bug", priority={"name": "Low"}))
    assert rca_rule_matches(scope, RULES) is True


def test_normalize_uppercases_project_and_merges_duplicates():
    rules, error = normalize_rca_rules([
        {"projectKey": "ops", "issueTypes": ["Bug"]},
        {"projectKey": "OPS", "issueTypes": ["bug", "Incident"]},
    ])
    assert error is None
    assert rules == [{"projectKey": "OPS", "issueTypes": ["Bug", "Incident"], **_OPEN}]


def test_normalize_keeps_same_project_when_limits_differ():
    rules, error = normalize_rca_rules([
        {"projectKey": "OPS", "issueTypes": ["Bug"], "priorities": ["High"]},
        {"projectKey": "OPS", "issueTypes": ["Bug"], "priorities": ["Low"]},
    ])
    assert error is None
    assert len(rules) == 2
    assert {tuple(rule["priorities"]) for rule in rules} == {("High",), ("Low",)}


def test_normalize_rejects_empty_types_bad_keys_and_broken_jql():
    rules, error = normalize_rca_rules([{"projectKey": "OPS", "issueTypes": []}])
    assert rules is None
    assert error

    rules, error = normalize_rca_rules([{"projectKey": "has space", "issueTypes": ["Bug"]}])
    assert rules is None
    assert error

    rules, error = normalize_rca_rules({"projectKey": "OPS"})
    assert rules is None
    assert error

    rules, error = normalize_rca_rules([{
        "projectKey": "OPS",
        "issueTypes": ["Bug"],
        "jql": "priority = Low) OR (summary is not EMPTY",
    }])
    assert rules is None
    assert error


def test_normalize_accepts_an_empty_list():
    rules, error = normalize_rca_rules([])
    assert error is None
    assert rules == []


def test_jql_rule_is_not_a_structured_match():
    rules = [{"projectKey": "OPS", "issueTypes": ["Bug"], "jql": '"Environment" = Production'}]
    scope = extract_webhook_scope(_issue("OPS", "Bug", key="OPS-9"))
    assert match_kind(scope, rules) == "jql"
    assert match_kind(scope, RULES) == "match"


def test_comment_in_auto_updates_filters_and_does_not_start_rca():
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": "Only investigate Bugs in OPS"},
        "issue": _issue("OPS", "Bug"),
    }
    assert decide_webhook(payload, "auto", RULES)["action"] == "filters"
    assert decide_webhook(payload, "manual", RULES)["action"] == "ignore"


def test_comment_on_a_project_with_no_rule_is_ignored_even_in_auto():
    # Auto only edits a project's own filter — it cannot bootstrap a brand new project.
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": "Only investigate Bugs here"},
        "issue": _issue("UNSEEN", "Bug"),
    }
    assert decide_webhook(payload, "auto", RULES)["action"] == "ignore"


def test_aurora_own_writeup_comment_is_not_a_filter_instruction():
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": "## Aurora RCA — summary of the investigation and OPS findings"},
        "issue": _issue("OPS", "Bug"),
    }
    assert decide_webhook(payload, "auto", RULES)["action"] == "ignore"


def test_comment_from_the_connected_account_is_not_a_filter_instruction():
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": "Only investigate Bugs in OPS", "author": {"accountId": "aurora-bot"}},
        "issue": _issue("OPS", "Bug"),
    }
    assert decide_webhook(payload, "auto", RULES, connected_account_id="aurora-bot")["action"] == "ignore"
    assert decide_webhook(payload, "auto", RULES, connected_account_id="someone-else")["action"] == "filters"


def test_empty_comment_does_not_start_rca():
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": "ok"},
        "issue": _issue("OPS", "Bug"),
    }
    assert decide_webhook(payload, "auto", RULES)["action"] == "ignore"


def test_issue_created_still_uses_the_filter():
    payload = {"webhookEvent": "jira:issue_created", "issue": _issue("OPS", "Bug")}
    assert decide_webhook(payload, "auto", RULES)["action"] == "rca"
    story = {"webhookEvent": "jira:issue_created", "issue": _issue("OPS", "Story")}
    assert decide_webhook(story, "auto", RULES)["action"] == "ignore"


class _Search:
    def __init__(self, issues=None, status=None):
        self._issues = issues or []
        self._status = status

    def search_issues(self, jql, fields=None, max_results=1):
        if self._status:
            import requests
            response = requests.Response()
            response.status_code = self._status
            raise requests.HTTPError(response=response)
        return {"issues": self._issues}


def test_jql_match_confirms_the_issue():
    assert jql_matches_issue(_Search([{"key": "OPS-1"}]), "OPS-1", ['priority = "High"']) is True
    assert jql_matches_issue(_Search([]), "OPS-1", ['priority = "High"']) is False


def test_jql_match_requires_the_returned_key_to_be_the_checked_issue():
    # A clause that escapes the key wrapper might still return a different issue — that's not a confirmation.
    assert jql_matches_issue(_Search([{"key": "OTHER-9"}]), "OPS-1", ['priority = "High"']) is False


def test_rejected_jql_is_a_miss_and_other_errors_propagate():
    assert jql_matches_issue(_Search(status=400), "OPS-1", ["???("]) is False
    try:
        jql_matches_issue(_Search(status=503), "OPS-1", ["priority = High"])
    except Exception as exc:
        assert getattr(exc.response, "status_code", None) == 503
    else:
        raise AssertionError("expected the Jira error to propagate")


def test_normalize_rejects_a_paren_hidden_inside_a_quoted_value():
    # Jira would group this as (summary ~ "()") OR (project is not EMPTY) — the OR escapes the key wrapper.
    rules, error = normalize_rca_rules([{
        "projectKey": "OPS",
        "issueTypes": ["Bug"],
        "jql": 'summary ~ "()" ) OR (project is not EMPTY',
    }])
    assert rules is None
    assert error


def test_normalize_accepts_balanced_parens_inside_a_quoted_value():
    rules, error = normalize_rca_rules([{
        "projectKey": "OPS",
        "issueTypes": ["Bug"],
        "jql": 'summary ~ "(prod)"',
    }])
    assert error is None
    assert rules[0]["jql"] == 'summary ~ "(prod)"'
