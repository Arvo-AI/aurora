"""Tests for "Sign in with Slack" (routes/slack/slack_signin.py).

Pins who gets an Aurora session from a Slack identity:

- The id_token nonce must match the one in our signed state, and Slack must
  say the email is verified.
- A linked identity (or the workspace installer) signs into that account.
- A full member of a connected workspace joins its org as a viewer; guests
  and Slack Connect users do not.
- An email-only match is linked only inside the same org and never for an
  admin, so a Slack workspace admin editing emails can't reach an Aurora admin.

Slack API and DB are mocked: no I/O.
"""

from __future__ import annotations

import base64
import json
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def signin_app(monkeypatch):
    for m in [m for m in sys.modules if m.startswith(("routes.slack.slack_signin",
                                                       "routes.slack.slack_signup",
                                                       "routes.slack.slack_routes",
                                                       "connectors.slack_connector.oauth"))]:
        del sys.modules[m]
    monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")

    import importlib
    from flask import Flask
    slack_routes = importlib.import_module("routes.slack.slack_routes")
    mod = importlib.import_module("routes.slack.slack_signin")
    signup = importlib.import_module("routes.slack.slack_signup")

    application = Flask(__name__)  # NOSONAR
    application.register_blueprint(slack_routes.slack_bp, url_prefix="/slack")
    application.register_blueprint(mod.slack_signin_bp, url_prefix="/slack")
    application.register_blueprint(signup.slack_signup_bp, url_prefix="/slack")
    return application, mod, signup


def _mock_db(fetchone_seq=()):
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_seq)
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    db_pool = MagicMock()
    db_pool.get_admin_connection.return_value = conn
    return db_pool, cur


def _id_token(nonce: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"nonce": nonce}).encode()).decode().rstrip("=")
    return f"h.{payload}.sig"


def _start(client):
    resp = client.get("/slack/signin/start")
    assert resp.status_code == 302
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(resp.location).query)
    return resp, q["state"][0], q["nonce"][0]


def _userinfo(email_verified=True, team_id="T1", email="Sam@Acme.io"):
    return {
        "ok": True, "sub": "U2", "https://slack.com/user_id": "U2",
        "https://slack.com/team_id": team_id, "email": email,
        "email_verified": email_verified, "name": "Sam",
    }


@contextmanager
def _oidc(mod, nonce, userinfo=None):
    token_resp = MagicMock()
    token_resp.json.return_value = {"ok": True, "access_token": "xoxp-1", "id_token": _id_token(nonce)}
    info_resp = MagicMock()
    info_resp.json.return_value = userinfo or _userinfo()
    with patch.object(mod.requests, "post", return_value=token_resp), \
         patch.object(mod.requests, "get", return_value=info_resp):
        yield


class TestStart:
    def test_redirects_to_slack_openid_with_verifiable_state(self, signin_app):
        app, mod, _ = signin_app
        resp, state, nonce = _start(app.test_client())
        assert resp.location.startswith("https://slack.com/openid/connect/authorize?")
        with app.test_request_context():
            assert mod.load_signin_state(state)["nonce"] == nonce

    def test_unavailable_when_hosted_signup_is_off(self, signin_app, monkeypatch):
        app, _, _ = signin_app
        monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "false")
        resp = app.test_client().get("/slack/signin/start")
        assert resp.location.endswith("/sign-in?error=slack_unavailable")

    def test_signin_and_signup_states_do_not_cross(self, signin_app):
        app, mod, signup = signin_app
        client = app.test_client()
        _, signin_state, _ = _start(client)
        signup_state = client.get("/slack/signup/start").location.split("state=", 1)[1]
        with app.test_request_context():
            assert signup.load_signup_state(signin_state) is None
            assert mod.load_signin_state(signup_state) is None


class TestCallback:
    def test_linked_identity_signs_in(self, signin_app):
        app, mod, _ = signin_app
        client = app.test_client()
        _, state, nonce = _start(client)
        db_pool, _ = _mock_db()
        with _oidc(mod, nonce), \
             patch.object(mod, "find_user_by_slack_identity", return_value="user-1") as find, \
             patch.object(mod, "db_pool", db_pool), \
             patch.object(mod, "mint_handoff", return_value="HANDOFF"):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "/sign-in?handoff=HANDOFF&via=slack_signin" in resp.location
        assert find.call_args.args == ("T1", "U2")

    def test_nonce_mismatch_is_rejected(self, signin_app):
        app, mod, _ = signin_app
        client = app.test_client()
        _, state, _nonce = _start(client)
        with _oidc(mod, "someone-elses-nonce"), \
             patch.object(mod, "find_user_by_slack_identity") as find:
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert resp.location.endswith("/sign-in?error=slack_identity")
        find.assert_not_called()

    def test_unverified_email_is_rejected(self, signin_app):
        app, mod, _ = signin_app
        client = app.test_client()
        _, state, nonce = _start(client)
        with _oidc(mod, nonce, _userinfo(email_verified=False)), \
             patch.object(mod, "find_user_by_slack_identity") as find:
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert resp.location.endswith("/sign-in?error=slack_identity")
        find.assert_not_called()

    def test_unconnected_workspace_is_offered_add_to_slack(self, signin_app):
        app, mod, _ = signin_app
        client = app.test_client()
        _, state, nonce = _start(client)
        with _oidc(mod, nonce), \
             patch.object(mod, "find_user_by_slack_identity", return_value=None), \
             patch.object(mod, "join_workspace_org", return_value=None):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert resp.location.endswith("/sign-in?error=slack_workspace_not_connected")


_IDENTITY = {"slack_user_id": "U2", "team_id": "T1", "team_name": "Acme",
             "email": "sam@acme.io", "name": "Sam"}


@contextmanager
def _workspace(mod, *, owner="owner-1", member=True, sso=False, db_rows=()):
    db_pool, cur = _mock_db(db_rows)
    with patch("routes.slack.slack_events_helpers.get_user_id_from_slack_team", return_value=owner), \
         patch("routes.slack.slack_events_helpers.get_workspace_member",
               return_value={"display_name": "Sam", "email": "sam@acme.io"} if member else None), \
         patch("connectors.slack_connector.client.get_slack_client_for_user", return_value=MagicMock()), \
         patch("utils.auth.stateless_auth.get_org_id_for_user", return_value="org-1"), \
         patch("utils.auth.saml_sso.org_requires_sso", return_value=sso), \
         patch("utils.auth.enforcer.assign_role_to_user", create=True) as assign, \
         patch("routes.audit_routes.record_audit_event", create=True), \
         patch.object(mod, "db_pool", db_pool):
        yield cur, assign


def _writes(cur):
    return [c.args[0] for c in cur.execute.call_args_list if c.args[0].lstrip().startswith(("INSERT", "UPDATE"))]


class TestJoinWorkspaceOrg:
    def test_not_connected_workspace_returns_none(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, owner=None) as (cur, _):
            assert mod.join_workspace_org(_IDENTITY) is None
        cur.execute.assert_not_called()

    def test_new_member_joins_as_viewer(self, signin_app):
        app, mod, _ = signin_app
        # fetchone: no account with this email, then the new user id.
        with app.test_request_context(), _workspace(mod, db_rows=[None, ("user-new",)]) as (cur, assign):
            assert mod.join_workspace_org(_IDENTITY) == "user-new"
        insert = next(c for c in cur.execute.call_args_list if "INSERT INTO users" in c.args[0])
        assert insert.args[1][3:5] == ("viewer", "org-1")
        assert assign.call_args.args == ("user-new", "viewer", "org-1")

    def test_guest_is_refused_without_writes(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, member=False) as (cur, _):
            resp = mod.join_workspace_org(_IDENTITY)
        assert resp.location.endswith("error=slack_not_member")
        cur.execute.assert_not_called()

    def test_sso_required_org_is_refused(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, sso=True) as (cur, _):
            resp = mod.join_workspace_org(_IDENTITY)
        assert resp.location.endswith("error=slack_sso_required")
        assert _writes(cur) == []

    def test_existing_admin_is_never_linked_by_email(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, db_rows=[("admin-1", "org-1", "admin", None)]) as (cur, _):
            resp = mod.join_workspace_org(_IDENTITY)
        assert resp.location.endswith("error=slack_admin_password")
        assert _writes(cur) == []

    def test_existing_member_of_same_org_is_linked(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, db_rows=[("user-7", "org-1", "editor", None)]) as (cur, assign):
            assert mod.join_workspace_org(_IDENTITY) == "user-7"
        assert any("UPDATE users SET slack_team_id" in w for w in _writes(cur))
        assign.assert_not_called()

    def test_account_in_another_org_is_refused(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, db_rows=[("user-9", "org-other", "viewer", None)]) as (cur, _):
            resp = mod.join_workspace_org(_IDENTITY)
        assert resp.location.endswith("error=account_exists")
        assert _writes(cur) == []

    def test_account_linked_to_another_slack_user_is_refused(self, signin_app):
        app, mod, _ = signin_app
        with app.test_request_context(), _workspace(mod, db_rows=[("user-7", "org-1", "viewer", "U-OTHER")]) as (cur, _):
            resp = mod.join_workspace_org(_IDENTITY)
        assert resp.location.endswith("error=slack_identity_mismatch")
        assert _writes(cur) == []
