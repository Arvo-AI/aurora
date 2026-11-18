"""What Aurora Watch writes to jsonb columns."""

import json

from services.watch.store import jsonb_dumps


def test_jsonb_text_drops_nul_but_keeps_text_that_only_looks_like_one():
    # jsonb rejects \u0000. A NUL is dropped; the six characters "\u0000" written in a
    # log line (an escaped backslash in JSON) are text and stay.
    value = {"line": "name=widget\x00\x00", "literal": "\\u0000 is how JSON spells NUL", "both": "\\\x00"}
    text = jsonb_dumps(value)
    assert json.loads(text) == {"line": "name=widget", "literal": "\\u0000 is how JSON spells NUL", "both": "\\"}


def test_a_state_saved_by_another_version_is_learned_again():
    from services.watch.detector import STATE_VERSION, current_state, new_state

    old = {**new_state(), "version": STATE_VERSION - 1, "polls": 40}
    assert current_state(old)["polls"] == 0 and current_state(None)["version"] == STATE_VERSION
    fresh = {**new_state(), "polls": 7}
    assert current_state(fresh) is fresh


def test_an_incident_leads_with_the_newest_suspects_only_when_its_own_finding_is_blamed_again():
    from services.watch.incidents import _merge_payload

    old = {"findings": [{"subject": "Deployment/shop/redis"}], "severity": "high", "message": "redis down",
           "suspected_changes": [{"object": f"Deployment/shop/a{i}", "at": "t1"} for i in range(3)]}
    newer_cause = {"object": "Deployment/shop/b", "at": "t2"}
    # A downstream symptom joining: its own (unrelated) suspects do not take over the incident.
    joined = _merge_payload(old, {"findings": [{"subject": "Deployment/shop/api"}], "severity": "high",
                                  "message": "api failing", "first_seen": "t2", "suspected_changes": [newer_cause]})
    assert [s["object"] for s in joined["suspected_changes"]][0] == "Deployment/shop/a0"
    assert joined["message"] == "redis down"
    # The incident's own finding, worse and blamed on a newer change: that change leads.
    reblamed = _merge_payload(old, {"findings": [{"subject": "Deployment/shop/redis"}], "severity": "critical",
                                    "message": "redis 0/1", "first_seen": "t2", "suspected_changes": [newer_cause]})
    assert [s["object"] for s in reblamed["suspected_changes"]][0] == "Deployment/shop/b"
    assert (reblamed["message"], reblamed["severity"]) == ("redis down", "critical")


def test_a_state_digested_with_another_key_is_learned_again():
    # The digest key (INTERNAL_API_SECRET) was rotated: every masked value would read as changed.
    from services.watch.detector import current_state, new_state

    assert current_state({**new_state(), "digest_id": "rotated!", "polls": 40})["polls"] == 0
