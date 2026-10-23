"""Slack @mentions run as the Aurora user behind the sender's email."""

from routes.slack.slack_events_helpers import (
    SLACK_ALLOW_UNREGISTERED_KEY,
    classify_slack_sender,
    get_workspace_member,
    org_allows_unregistered_slack,
)
from routes.user_preferences import ORG_SCOPED_PREFERENCE_KEYS
from utils.auth.connector_access import (
    Access,
    Grant,
    access_for,
    connector_access_cache_token,
    pop_connector_access,
    push_connector_access,
)

WORKSPACE = "uid-owner"
ADMIN = "uid-admin"


def test_preference_key_is_stored_for_the_org():
    assert SLACK_ALLOW_UNREGISTERED_KEY in ORG_SCOPED_PREFERENCE_KEYS


def test_email_match_runs_as_that_account_even_when_guests_are_off():
    user_id, as_viewer, refusal = classify_slack_sender(
        matched_user_id=ADMIN,
        installer_user_id=WORKSPACE,
        is_full_member=True,
        workspace_user_id=WORKSPACE,
        allow_unregistered=False,
    )
    assert (user_id, as_viewer, refusal) == (ADMIN, False, None)


def test_installer_is_used_when_the_profile_email_does_not_match():
    user_id, as_viewer, refusal = classify_slack_sender(
        matched_user_id=None,
        installer_user_id=WORKSPACE,
        is_full_member=True,
        workspace_user_id=WORKSPACE,
        allow_unregistered=False,
    )
    assert (user_id, as_viewer, refusal) == (WORKSPACE, False, None)


def test_unregistered_member_is_refused_when_the_org_disallows_it():
    user_id, as_viewer, refusal = classify_slack_sender(
        matched_user_id=None,
        installer_user_id=None,
        is_full_member=True,
        workspace_user_id=WORKSPACE,
        allow_unregistered=False,
    )
    assert user_id is None
    assert as_viewer is False
    assert "without an account" in refusal


def test_unregistered_member_runs_as_a_general_viewer_when_allowed():
    user_id, as_viewer, refusal = classify_slack_sender(
        matched_user_id=None,
        installer_user_id=None,
        is_full_member=True,
        workspace_user_id=WORKSPACE,
        allow_unregistered=True,
    )
    assert (user_id, as_viewer, refusal) == (WORKSPACE, True, None)


def test_guest_or_external_slack_user_is_refused():
    user_id, as_viewer, refusal = classify_slack_sender(
        matched_user_id=None,
        installer_user_id=None,
        is_full_member=False,
        workspace_user_id=WORKSPACE,
        allow_unregistered=True,
    )
    assert user_id is None
    assert "members of this Slack workspace" in refusal
    assert as_viewer is False


class _Slack:
    def __init__(self, user):
        self.user = user

    def _make_request(self, method, path, params):
        assert path == "users.info"
        return {"ok": True, "user": self.user}


def test_full_member_includes_the_profile_email():
    member = get_workspace_member(
        _Slack({"team_id": "T1", "profile": {"display_name": "Ada", "email": "ada@example.com"}}),
        "U1",
        "T1",
    )
    assert member == {"display_name": "Ada", "email": "ada@example.com"}


def test_slack_guest_is_not_a_workspace_member():
    member = get_workspace_member(
        _Slack({"team_id": "T1", "is_restricted": True, "profile": {"email": "ada@example.com"}}),
        "U1",
        "T1",
    )
    assert member is None


def test_missing_guest_preference_allows_them(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.stateless_auth.get_org_preference",
        lambda org_id, key, default=None, strict=False: default,
    )
    assert org_allows_unregistered_slack("org-1") is True


def test_guest_preference_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.stateless_auth.get_org_preference",
        lambda org_id, key, default=None, strict=False: False,
    )
    assert org_allows_unregistered_slack("org-1") is False


def test_guest_preference_read_error_denies(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("utils.auth.stateless_auth.get_org_preference", boom)
    assert org_allows_unregistered_slack("org-1") is False


def test_no_org_does_not_allow_guests():
    assert org_allows_unregistered_slack("") is False


def test_pinned_access_is_a_general_viewer_and_does_not_leak():
    viewer = Access([Grant("cloudflare", "", "", "admin")], ["viewer"])
    token = push_connector_access(viewer)
    try:
        access = access_for("uid-owner")
        assert access.may_use("cloudflare") is False
        assert access.may_use("datadog") is True
        assert access.checked_as() == "general viewer"
        assert connector_access_cache_token() == viewer.revision()
    finally:
        pop_connector_access(token)
    assert connector_access_cache_token() == ""
