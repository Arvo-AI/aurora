from utils.auth.connector_access import Access, pin_sender_access
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
    assert message_starts_chat("", {"trigger_rca": True}) is True
    assert message_starts_chat("", {"attachments": [{"name": "log.txt"}]}) is True


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


def test_editors_modify_memory_only_when_the_org_allows(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["editor"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_memory_edits", lambda _org: False)
    assert viewer_may_edit_memory("uid", "org") is False
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_memory_edits", lambda _org: True)
    assert viewer_may_edit_memory("uid", "org") is True


def test_a_user_with_no_role_cannot_modify_memory(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: [])
    assert viewer_may_edit_memory("uid", "org") is False


def test_a_pinned_slack_guest_is_a_viewer_even_when_the_session_user_is_an_admin(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda *_a, **_k: ["admin"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", lambda _org: False)
    monkeypatch.setattr(
        "utils.auth.connector_access.general_viewer_access",
        lambda _uid: Access([], ["viewer"]),
    )
    with pin_sender_access("admin-uid", "general_viewer"):
        assert viewer_may_edit_memory("admin-uid", "org") is False
    # The pin ends with the chat, so the same user is an admin again.
    assert viewer_may_edit_memory("admin-uid", "org") is True
    with pin_sender_access("admin-uid", None):
        assert viewer_may_edit_memory("admin-uid", "org") is True


def test_a_pinned_viewer_can_edit_when_the_org_allows(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda *_a, **_k: ["admin"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", lambda _org: True)
    monkeypatch.setattr(
        "utils.auth.connector_access.general_viewer_access",
        lambda _uid: Access([], ["viewer"]),
    )
    with pin_sender_access("admin-uid", "general_viewer"):
        assert viewer_may_edit_memory("admin-uid", "org") is True


def test_admins_always_modify_memory(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["admin"])

    def _unexpected(_org):
        raise AssertionError("admin should not read a memory switch")

    monkeypatch.setattr("utils.auth.viewer_run.org_allows_memory_edits", _unexpected)
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_memory_edits", _unexpected)
    assert viewer_may_edit_memory("uid", "org") is True
