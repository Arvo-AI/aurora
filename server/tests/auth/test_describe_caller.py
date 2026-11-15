"""The prompt names the asker's Aurora level and team."""

from utils.auth.connector_access import Access, describe_caller, pop_connector_access, push_connector_access


def _roles(monkeypatch, roles):
    monkeypatch.setattr("utils.auth.stateless_auth.get_org_id_for_user", lambda _uid: "org")
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda *_a, **_k: roles)


def test_a_viewer_on_a_team_is_named_with_that_level_and_team(monkeypatch):
    _roles(monkeypatch, ["viewer"])
    monkeypatch.setattr("utils.auth.connector_access.load_team_rules", lambda _uid: ({"t1"}, []))
    monkeypatch.setattr("utils.auth.connector_access._team_names", lambda _uid, _ids: {"t1": "Database"})
    text = describe_caller("uid")
    assert text.startswith("The person asking is a viewer on the Database team.")
    assert "job title in memory is not their role" in text


def test_an_editor_with_no_team_uses_an(monkeypatch):
    _roles(monkeypatch, ["editor"])
    monkeypatch.setattr("utils.auth.connector_access.load_team_rules", lambda _uid: (set(), []))
    text = describe_caller("uid")
    assert text.startswith("The person asking is an editor with no team.")


def test_a_slack_guest_is_a_general_viewer_and_not_the_session_user(monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("a guest must not use the connector owner's role")

    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", _forbidden)
    token = push_connector_access(Access([], ["viewer"]))
    try:
        text = describe_caller("admin-uid")
    finally:
        pop_connector_access(token)
    assert text.startswith("The person asking is a general viewer with no Aurora account and no team.")
