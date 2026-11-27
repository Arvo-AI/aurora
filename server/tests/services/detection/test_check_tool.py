"""Who may make a check trusted from the start, and what the off switch stops.

A check that opens incidents without a record is only safe when a person asked
for it, so the cases here are the ways a model could claim that on its own.
"""

import json
from types import SimpleNamespace

import pytest

from chat.backend.agent.tools import detection_check_tool as tool


def _in_conversation(monkeypatch, state):
    import utils.cloud.cloud_utils as cloud_utils

    monkeypatch.setattr(cloud_utils, "get_state_context", lambda: state)


def test_a_person_asking_in_a_live_conversation_vouches_for_the_check(monkeypatch):
    _in_conversation(monkeypatch, SimpleNamespace(is_background=False))
    assert tool._asked_by_a_person({"kind": "person", "ref": "sam"})


@pytest.mark.parametrize("state", [
    SimpleNamespace(is_background=True),   # an investigation or a scheduled action
    SimpleNamespace(),                     # a state that does not say
    None,                                  # no conversation at all
])
def test_a_run_with_nobody_in_it_cannot_claim_a_person_asked(monkeypatch, state):
    _in_conversation(monkeypatch, state)
    assert not tool._asked_by_a_person({"kind": "person", "ref": "sam"})


def test_a_check_aurora_came_up_with_is_not_vouched_for_even_in_a_conversation(monkeypatch):
    _in_conversation(monkeypatch, SimpleNamespace(is_background=False))
    assert not tool._asked_by_a_person({"kind": "incident", "ref": "INC 1"})


@pytest.mark.parametrize("call", [
    lambda: tool.save_detection_check(user_id="u1", name="x"),
    lambda: tool.list_detection_checks(user_id="u1"),
    lambda: tool.retire_detection_check("x", "why", user_id="u1"),
])
def test_on_an_install_that_never_turned_the_feature_on_no_tool_touches_the_store(monkeypatch, call):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)

    def _no_database(*_args, **_kwargs):
        raise AssertionError("the store was reached with the feature off")

    monkeypatch.setattr(tool, "_connection", _no_database)
    monkeypatch.setattr(tool, "_may_write", lambda _user: True)
    assert "turned off" in json.loads(call())["error"]


@pytest.mark.parametrize("checks, chat, reads", [
    (None, None, False),
    (None, "true", False),
    ("true", None, False),
    ("true", "false", False),
    ("false", "true", False),
    ("true", "true", True),
])
def test_chat_is_only_read_on_an_install_that_turned_on_both_the_feature_and_the_reading(
        monkeypatch, checks, chat, reads):
    from services import detection
    for name, value in (("DETECTION_CHECKS_ENABLED", checks), ("DETECTION_CHAT_INTAKE_ENABLED", chat)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert detection.chat_intake_enabled() is reads
