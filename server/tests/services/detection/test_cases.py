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


def test_a_long_name_cannot_push_the_title_past_the_incident_limit():
    assert len(case_title(dict(_CHECK, name="x" * 255, asset="y" * 512))) <= 200


def test_the_description_carries_what_was_seen_and_why_the_check_exists():
    description = case_description(_CHECK, "3")
    assert "Value seen: 3" in description
    assert "INC 12" in description
    assert _CHECK["command"] in description


def test_an_unrecorded_origin_is_stated_rather_than_invented():
    assert "not recorded" in origin_sentence({})
    assert "not recorded" in origin_sentence({"kind": "mystery"})
