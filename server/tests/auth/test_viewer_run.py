from utils.auth.connector_access import Access, pin_sender_access
from utils.auth.viewer_run import (
    flag_enabled,
    message_starts_chat,
    org_allows_viewers,
    slack_caller_may_run,
    viewer_may_edit_memory,
    viewer_may_run,
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


def test_unset_viewer_chat_stays_on(monkeypatch):
    seen = {}

    def _flag(_org, key, default=False):
        seen["key"] = key
        seen["default"] = default
        return default

    monkeypatch.setattr("utils.auth.viewer_run._org_flag", _flag)
    assert org_allows_viewers("org") is True
    assert seen == {"key": "viewer_can_chat", "default": True}


def test_a_user_with_no_role_cannot_chat(monkeypatch):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: [])
    assert viewer_may_run("uid", "org") is False


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


# --- conversation audit ------------------------------------------------------

from utils.auth.viewer_run import may_audit_chats  # noqa: E402


def _roles(monkeypatch, roles):
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda *_a, **_k: roles)


def test_admins_may_audit_whatever_the_setting(monkeypatch):
    _roles(monkeypatch, ["admin"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_audit", lambda _org: False)
    assert may_audit_chats("u", "org") is True


def test_editors_may_audit_only_when_the_org_allows(monkeypatch):
    _roles(monkeypatch, ["editor"])
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_audit", lambda _org: False)
    assert may_audit_chats("u", "org") is False
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_audit", lambda _org: True)
    assert may_audit_chats("u", "org") is True


def test_viewers_and_roleless_users_never_audit(monkeypatch):
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_audit", lambda _org: True)
    for roles in (["viewer"], [], None):
        _roles(monkeypatch, roles)
        assert may_audit_chats("u", "org") is False


def test_audit_setting_defaults_to_admins_only(monkeypatch):
    seen = {}

    def _flag(_org, key, default=False):
        seen[key] = default
        return default

    monkeypatch.setattr("utils.auth.viewer_run._org_flag", _flag)
    from utils.auth.viewer_run import org_allows_editor_audit
    assert org_allows_editor_audit("org") is False and seen == {"editor_can_audit_chats": False}


def test_a_slack_guest_never_audits_even_under_an_admin_owner(monkeypatch):
    _roles(monkeypatch, ["admin"])
    monkeypatch.setattr(
        "utils.auth.connector_access.general_viewer_access", lambda _uid: Access([], ["viewer"]),
    )
    with pin_sender_access("admin-uid", "general_viewer"):
        assert may_audit_chats("admin-uid", "org") is False
    assert may_audit_chats("admin-uid", "org") is True


def test_role_lookup_error_or_missing_ids_mean_no(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("casbin down")

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", boom)
    assert may_audit_chats("u", "org") is False
    assert may_audit_chats("", "org") is False and may_audit_chats("u", "") is False


def test_a_failed_setting_read_keeps_editors_out(monkeypatch):
    _roles(monkeypatch, ["editor"])

    class Pool:
        def get_admin_connection(self):
            raise RuntimeError("db down")

    monkeypatch.setattr("utils.db.connection_pool.db_pool", Pool())
    assert may_audit_chats("u", "org") is False
