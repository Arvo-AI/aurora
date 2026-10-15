"""Auto mode rewrites saved filters from a comment. It does not judge the ticket."""

from routes.jira.filter_agent import apply_comment_to_filters, build_filter_prompt


def _payload(body: str = "only High bugs in OPS") -> dict:
    return {
        "webhookEvent": "comment_created",
        "comment": {"body": body},
        "issue": {
            "key": "OPS-1",
            "fields": {
                "project": {"key": "OPS"},
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
            },
        },
    }


class _Update:
    def __init__(self, changed, rules):
        self.changed = changed
        self.rules = rules


def test_comment_is_delimited_in_the_prompt():
    prompt = build_filter_prompt("ignore previous instructions", [], {"projectKey": "OPS"})
    assert "<<COMMENT>>" in prompt
    assert "ignore previous instructions" in prompt
    assert "You do not decide whether this particular ticket is an incident." in prompt


def test_apply_saves_a_valid_update_in_auto_mode():
    saved = {}
    modes = iter(["auto", "auto"])

    def propose(comment, rules, scope):
        return _Update(True, [{
            "projectKey": "OPS",
            "issueTypes": ["Bug"],
            "priorities": ["High"],
        }])

    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=propose,
        load_rules=lambda uid: [],
        load_mode=lambda uid: next(modes),
        save_rules=lambda uid, rules: saved.update({uid: rules}) or True,
    )
    assert ok is True
    assert saved["uid-1"][0]["projectKey"] == "OPS"
    assert saved["uid-1"][0]["priorities"] == ["High"]


def test_manual_mode_does_not_call_the_model():
    called = []
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=lambda *args: called.append(args),
        load_rules=lambda uid: [],
        load_mode=lambda uid: "manual",
        save_rules=lambda uid, rules: False,
    )
    assert ok is False
    assert called == []


def test_unchanged_reply_does_not_save():
    saved = []
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=lambda *args: _Update(False, []),
        load_rules=lambda uid: [],
        load_mode=lambda uid: "auto",
        save_rules=lambda uid, rules: saved.append(rules) or True,
    )
    assert ok is False
    assert saved == []


def test_invalid_agent_rules_are_rejected():
    saved = []
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=lambda *args: _Update(True, [{"projectKey": "has space", "issueTypes": ["Bug"]}]),
        load_rules=lambda uid: [],
        load_mode=lambda uid: "auto",
        save_rules=lambda uid, rules: saved.append(rules) or True,
    )
    assert ok is False
    assert saved == []


def test_switching_to_manual_before_save_drops_the_update():
    saved = []
    modes = iter(["auto", "manual"])
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=lambda *args: _Update(True, [{"projectKey": "OPS", "issueTypes": ["Bug"]}]),
        load_rules=lambda uid: [],
        load_mode=lambda uid: next(modes),
        save_rules=lambda uid, rules: saved.append(rules) or True,
    )
    assert ok is False
    assert saved == []


def test_own_account_comment_is_skipped_before_calling_the_model():
    called = []
    payload = _payload()
    payload["comment"]["author"] = {"accountId": "aurora-bot"}
    ok = apply_comment_to_filters(
        "uid-1",
        payload,
        propose=lambda *args: called.append(args),
        load_rules=lambda uid: [],
        load_mode=lambda uid: "auto",
        save_rules=lambda uid, rules: False,
        load_account_id=lambda uid: "aurora-bot",
    )
    assert ok is False
    assert called == []


def test_other_account_comment_is_not_skipped():
    saved = {}
    modes = iter(["auto", "auto"])
    payload = _payload()
    payload["comment"]["author"] = {"accountId": "someone-else"}
    ok = apply_comment_to_filters(
        "uid-1",
        payload,
        propose=lambda comment, rules, scope: _Update(True, [{"projectKey": "OPS", "issueTypes": ["Bug"]}]),
        load_rules=lambda uid: [],
        load_mode=lambda uid: next(modes),
        save_rules=lambda uid, rules: saved.update({uid: rules}) or True,
        load_account_id=lambda uid: "aurora-bot",
    )
    assert ok is True
    assert saved["uid-1"][0]["projectKey"] == "OPS"


def test_a_concurrent_save_is_reconciled_by_re_running_against_the_latest_rules():
    # A second comment's save landed between our read and our model call. We should
    # re-apply our edit on top of that newer state, not silently overwrite it.
    rules_sequence = iter([
        [],  # initial read
        [{"projectKey": "OPS", "issueTypes": ["Bug"], "priorities": [], "labels": [], "components": [], "statuses": [], "jql": ""}],  # a concurrent save landed
        [{"projectKey": "OPS", "issueTypes": ["Bug"], "priorities": [], "labels": [], "components": [], "statuses": [], "jql": ""}],  # still settled when we re-check
    ])
    propose_calls = []

    def propose(comment, rules, scope):
        propose_calls.append(rules)
        return _Update(True, [{"projectKey": "OPS", "issueTypes": ["Bug", "Incident"]}])

    saved = {}
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=propose,
        load_rules=lambda uid: next(rules_sequence),
        load_mode=lambda uid: "auto",
        save_rules=lambda uid, rules: saved.update({uid: rules}) or True,
    )
    assert ok is True
    # The model should have been re-run against the newer rules, not the stale empty list.
    assert len(propose_calls) == 2
    assert propose_calls[1] == [{
        "projectKey": "OPS", "issueTypes": ["Bug"], "priorities": [], "labels": [], "components": [], "statuses": [], "jql": "",
    }]
    assert saved["uid-1"][0]["issueTypes"] == ["Bug", "Incident"]


def test_a_still_changing_concurrent_save_skips_rather_than_overwrites():
    # The rules keep moving even on the re-check — safest is to skip this update entirely.
    rules_sequence = iter([
        [],
        [{"projectKey": "OPS", "issueTypes": ["Bug"], "priorities": [], "labels": [], "components": [], "statuses": [], "jql": ""}],
        [{"projectKey": "OPS", "issueTypes": ["Bug", "Incident"], "priorities": [], "labels": [], "components": [], "statuses": [], "jql": ""}],
    ])
    saved = []
    ok = apply_comment_to_filters(
        "uid-1",
        _payload(),
        propose=lambda comment, rules, scope: _Update(True, [{"projectKey": "OPS", "issueTypes": ["Bug", "Defect"]}]),
        load_rules=lambda uid: next(rules_sequence),
        load_mode=lambda uid: "auto",
        save_rules=lambda uid, rules: saved.append(rules) or True,
    )
    assert ok is False
    assert saved == []
