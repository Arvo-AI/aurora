"""Automatic Jira RCA runs only for an opted-in project and ticket type."""

from routes.jira.jira_routes import (
    extract_webhook_scope,
    normalize_rca_rules,
    rca_rule_matches,
)


RULES = [
    {"projectKey": "OPS", "issueTypes": ["Bug", "Incident"]},
    {"projectKey": "RD", "issueTypes": ["Defect"]},
]


def _issue(project: str, issue_type: str, key: str = "") -> dict:
    return {
        "key": key or f"{project}-1",
        "fields": {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
        },
    }


def test_matching_project_and_type_triggers():
    project, issue_type = extract_webhook_scope(_issue("OPS", "Bug"))
    assert rca_rule_matches(project, issue_type, RULES) is True


def test_match_is_case_insensitive():
    project, issue_type = extract_webhook_scope(_issue("ops", "incident"))
    assert rca_rule_matches(project, issue_type, RULES) is True


def test_same_project_wrong_type_does_not_trigger():
    project, issue_type = extract_webhook_scope(_issue("OPS", "Story"))
    assert rca_rule_matches(project, issue_type, RULES) is False


def test_other_project_does_not_trigger():
    project, issue_type = extract_webhook_scope(_issue("OTHER", "Bug"))
    assert rca_rule_matches(project, issue_type, RULES) is False


def test_no_rules_means_nothing_triggers():
    project, issue_type = extract_webhook_scope(_issue("OPS", "Bug"))
    assert rca_rule_matches(project, issue_type, []) is False
    assert rca_rule_matches(project, issue_type, None) is False


def test_issue_key_supplies_project_when_field_is_missing():
    issue = {"key": "OPS-42", "fields": {"issuetype": {"name": "Bug"}}}
    project, issue_type = extract_webhook_scope(issue)
    assert (project, issue_type) == ("OPS", "Bug")
    assert rca_rule_matches(project, issue_type, RULES) is True


def test_missing_type_does_not_trigger():
    project, issue_type = extract_webhook_scope({"key": "OPS-1", "fields": {"project": {"key": "OPS"}}})
    assert rca_rule_matches(project, issue_type, RULES) is False


def test_normalize_uppercases_project_and_merges_duplicates():
    rules, error = normalize_rca_rules([
        {"projectKey": "ops", "issueTypes": ["Bug"]},
        {"projectKey": "OPS", "issueTypes": ["bug", "Incident"]},
    ])
    assert error is None
    assert rules == [{"projectKey": "OPS", "issueTypes": ["Bug", "Incident"]}]


def test_normalize_rejects_empty_types_and_bad_keys():
    rules, error = normalize_rca_rules([{"projectKey": "OPS", "issueTypes": []}])
    assert rules is None
    assert error

    rules, error = normalize_rca_rules([{"projectKey": "has space", "issueTypes": ["Bug"]}])
    assert rules is None
    assert error

    rules, error = normalize_rca_rules({"projectKey": "OPS"})
    assert rules is None
    assert error


def test_normalize_accepts_an_empty_list():
    rules, error = normalize_rca_rules([])
    assert error is None
    assert rules == []
