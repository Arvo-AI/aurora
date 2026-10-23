from utils.auth.viewer_run import (
    flag_enabled,
    message_starts_chat,
    slack_caller_may_run,
    viewer_may_edit_memory,
    viewer_only,
)


def test_missing_preference_uses_the_default():
    assert flag_enabled(None, False) is False
    assert flag_enabled(None, True) is True


def test_only_an_explicit_false_turns_a_flag_off():
    assert flag_enabled("false", True) is False
    assert flag_enabled("true", False) is True
    assert flag_enabled("no", True) is False


def test_an_action_with_no_question_still_starts_a_chat():
    assert message_starts_chat("", {"trigger_action": "act-1"}) is True
    assert message_starts_chat("", {"direct_tool_call": {"name": "cloud_exec"}}) is True
    assert message_starts_chat("", {}) is False


def test_unlinked_slack_member_is_not_treated_as_a_viewer_here():
    # Guests are allowed only by the Slack manage toggle, before this check.
    assert slack_caller_may_run("org", "uid", unlinked=True) is False


def test_editor_and_admin_are_not_viewer_only():
    assert viewer_only(["viewer"]) is True
    assert viewer_only(["editor"]) is False
    assert viewer_only(["admin"]) is False
    assert viewer_only(["viewer", "editor"]) is False
    assert viewer_only([]) is False


def test_viewers_modify_memory_only_when_the_org_allows(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["viewer"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", lambda _org: False)
    assert viewer_may_edit_memory("uid", "org") is False
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", lambda _org: True)
    assert viewer_may_edit_memory("uid", "org") is True


def test_editors_always_modify_memory(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["editor"])
    def _unexpected(_org):
        raise AssertionError("editor should not read the switch")
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", _unexpected)
    assert viewer_may_edit_memory("uid", "org") is True
