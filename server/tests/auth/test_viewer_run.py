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


def _memory_switches(monkeypatch, **values):
    """Stub the stored memory switches. A value of None simulates a failed read."""
    keys = {"viewer": "viewer_can_edit_memory", "editor": "editor_can_edit_memory"}
    by_key = {keys[name]: value for name, value in values.items()}
    monkeypatch.setattr(
        "utils.auth.viewer_run._org_flag_state",
        lambda _org, key, default=False: by_key.get(key, default),
    )


def test_memory_refusal_names_the_viewer_role_and_switch(monkeypatch):
    from utils.auth.viewer_run import memory_edit_refusal

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["viewer"])
    _memory_switches(monkeypatch, viewer=False)
    assert memory_edit_refusal("uid", "org") == (
        "You don't have access to modify memories as a viewer. Ask an admin to change this "
        "permission in org Settings, on the Access tab. The switch is 'Viewers can modify memories'."
    )
    _memory_switches(monkeypatch, viewer=True)
    assert memory_edit_refusal("uid", "org") is None


def test_memory_refusal_names_the_editor_switch(monkeypatch):
    from utils.auth.viewer_run import memory_edit_refusal

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["editor"])
    _memory_switches(monkeypatch, editor=False)
    reason = memory_edit_refusal("uid", "org")
    assert reason.startswith("You don't have access to modify memories as an editor.")
    assert reason.endswith("The switch is 'Editors can modify memories'.")
    # Unset stays on for editors.
    _memory_switches(monkeypatch)
    assert memory_edit_refusal("uid", "org") is None


def test_memory_refusal_explains_a_slack_guest(monkeypatch):
    from utils.auth.viewer_run import memory_edit_refusal

    # The connector owner is an admin, but the guest pin is what counts.
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["admin"])
    _memory_switches(monkeypatch, viewer=False)
    monkeypatch.setattr(
        "utils.auth.connector_access.general_viewer_access",
        lambda _uid: Access([], ["viewer"]),
    )
    with pin_sender_access("admin-uid", "general_viewer"):
        reason = memory_edit_refusal("admin-uid", "org")
    assert reason.startswith("You don't have access to modify memories as a general viewer.")


def test_memory_refusal_without_a_role(monkeypatch):
    from utils.auth.viewer_run import memory_edit_refusal

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: [])
    assert memory_edit_refusal("uid", "org") == (
        "You don't have access to modify memories because you have no role in this "
        "organization. Ask an admin to assign you one in org Settings."
    )


def test_memory_refusal_without_an_org():
    from utils.auth.viewer_run import memory_edit_refusal

    assert memory_edit_refusal("uid", "") == (
        "Aurora could not determine this person's organization, so memory was not "
        "modified. Try again. If it keeps failing, check that their account belongs "
        "to an organization."
    )


def test_memory_refusal_reports_a_failed_switch_read(monkeypatch):
    from utils.auth.viewer_run import memory_edit_refusal, viewer_may_edit_memory

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda _uid, _org: ["viewer"])
    _memory_switches(monkeypatch, viewer=None)
    # Still fails closed, but is not reported as the switch being off.
    assert viewer_may_edit_memory("uid", "org") is False
    assert memory_edit_refusal("uid", "org") == (
        "Aurora could not read the 'Viewers can modify memories' setting, so memory was not "
        "modified. This is a temporary read failure, not a permission change. Try again shortly."
    )


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
