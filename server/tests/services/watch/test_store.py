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


def test_an_extended_incident_leads_with_the_newest_suspects():
    from services.watch.incidents import _merge_payload

    old = {"findings": [], "severity": "high", "message": "old",
           "suspected_changes": [{"object": f"Deployment/shop/a{i}", "at": "t1"} for i in range(3)]}
    new = {"findings": [], "severity": "critical", "message": "new", "first_seen": "t2",
           "suspected_changes": [{"object": "Deployment/shop/b", "at": "t2"}]}
    merged = _merge_payload(old, new)
    assert [s["object"] for s in merged["suspected_changes"]] == [
        "Deployment/shop/b", "Deployment/shop/a0", "Deployment/shop/a1"]
    assert (merged["message"], merged["severity"]) == ("new", "critical")
