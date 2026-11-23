"""What the opening of a case tells the people who receive it.

When Aurora opens a case on its own, the first question is what made it look.
The title is the one field the Slack routing prompt always carries, so the
trigger has to be readable from it alone.
"""

from services.detection.cases import case_description, case_title, origin_sentence

_CHECK = {
    "name": "checkout crashloops",
    "revision": 2,
    "asset": "checkout",
    "prediction": "checkout pods enter CrashLoopBackOff",
    "command": "kubectl get pods -n checkout --no-headers | grep -c CrashLoopBackOff",
    "assertion": {"expect": "number", "op": "eq", "value": 0},
    "origin": {"kind": "incident", "ref": "INC 12"},
}


def test_the_title_alone_says_a_check_fired_and_on_what():
    title = case_title(_CHECK)
    assert "checkout crashloops" in title
    assert "checkout" in title
    assert "check" in title.lower()


def test_a_long_name_cannot_push_the_asset_out_of_the_title():
    title = case_title(dict(_CHECK, name="x" * 255, asset="checkout"))
    assert len(title) <= 200
    assert title.endswith("failing on checkout")
    assert len(case_title(dict(_CHECK, name="x" * 255, asset="y" * 512))) <= 200


def test_the_description_carries_what_was_seen_and_why_the_check_exists():
    description = case_description(_CHECK, "3")
    assert "Value seen: 3" in description
    assert "INC 12" in description
    assert _CHECK["command"] in description


def test_an_unrecorded_origin_is_stated_rather_than_invented():
    assert "not recorded" in origin_sentence({})
    assert "not recorded" in origin_sentence({"kind": "mystery"})


def test_an_incident_is_refused_rather_than_created_in_another_org(monkeypatch):
    # A check's owner moved orgs. Opening the case as them would put the old
    # org's command and output into the new org's incident.
    import json

    from chat.backend.agent.tools import trigger_rca_tool

    monkeypatch.setattr("utils.auth.stateless_auth.get_org_id_for_user", lambda _uid: "org-b")
    result = json.loads(trigger_rca_tool.trigger_rca(
        issue_description="check failed", user_id="user-1", expected_org_id="org-a",
    ))
    assert "error" in result
    assert "incident_id" not in result


def test_a_windowed_check_is_handed_over_with_the_span_that_failed():
    # The stored command has placeholders. An investigation cannot run that.
    from datetime import datetime, timezone

    windowed = dict(
        _CHECK, window_seconds=600, lag_seconds=120,
        command="logging read 'timestamp>={from} AND timestamp<={to}' | jq length",
    )
    fired_at = datetime(2026, 11, 10, 14, 15, tzinfo=timezone.utc)
    description = case_description(windowed, "81", fired_at)
    assert "timestamp>=2026-11-10T14:03:00Z AND timestamp<=2026-11-10T14:13:00Z" in description
    assert "{from}" not in description
