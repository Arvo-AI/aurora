"""Tests for the one-click "Add to Slack" signup flow (routes/slack/slack_signup.py).

Pins the security contract:

- The flow is OFF unless ``HOSTED_SIGNUP_ENABLED=true`` and Slack OAuth
  credentials exist.
- The in-app connect flow (state = bare user id) shares ``/slack/callback``
  and must never be routed into signup; a forged/expired signup state falls
  through to the in-app path, which provisions nothing.
- An installer already connected to Aurora is logged into THAT account; an
  email-only match redirects to sign-in; a workspace owned by another org is
  never connected to a second one (the installer joins that org instead).
- Clicking Cancel on Slack's screen goes back to the website.

Slack API and DB are mocked — no I/O.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def slack_app(monkeypatch):
    for m in [m for m in sys.modules if m.startswith(("routes.slack.slack_signup",
                                                       "routes.slack.slack_signin",
                                                       "routes.slack.slack_routes",
                                                       "connectors.slack_connector.oauth"))]:
        del sys.modules[m]
    monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("SLACK_CLIENT_ID", "cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")

    import importlib
    from flask import Flask
    # import_module, not ``from routes.slack import ...``: the package keeps a
    # stale attribute for evicted submodules, and the callback's lazy import
    # must resolve to the same module object these tests patch.
    slack_routes = importlib.import_module("routes.slack.slack_routes")
    mod = importlib.import_module("routes.slack.slack_signup")

    application = Flask(__name__)  # NOSONAR
    application.register_blueprint(slack_routes.slack_bp, url_prefix="/slack")
    application.register_blueprint(mod.slack_signup_bp, url_prefix="/slack")
    monkeypatch.setattr(mod.flask, "render_template",
                        lambda *a, **kw: f"ERROR:{kw.get('error', '')}")
    return application, mod, slack_routes


def _mock_db(fetchone_seq):
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


_TOKEN_DATA = {
    "ok": True,
    "access_token": "xoxb-1",
    "bot_user_id": "UBOT",
    "team": {"id": "T1", "name": "Acme"},
    "authed_user": {"id": "U1"},
}


def _users_info(team_id="T1", email="Dana@Acme.io", **user_overrides):
    resp = MagicMock(status_code=200)
    user = {"id": "U1", "team_id": team_id, "profile": {"email": email, "real_name": "Dana"}}
    user.update(user_overrides)
    resp.json.return_value = {"ok": True, "user": user}
    return resp


def _start_state(client, query=""):
    resp = client.get(f"/slack/signup/start{query}")
    assert resp.status_code == 302
    return resp.location.split("state=", 1)[1]


@contextmanager
def _slack(mod, *, returning_user=None, team_owner=None, joined=None, users_info=None, db_rows=()):
    """``joined`` is what ``join_workspace_org`` returns: None when the
    workspace is not connected to any org yet."""
    db_pool, cur = _mock_db(db_rows)
    signin = sys.modules["routes.slack.slack_signin"]
    handoff_db, _ = _mock_db([])
    with patch("connectors.slack_connector.oauth.exchange_code_for_token", return_value=_TOKEN_DATA), \
         patch.object(mod.requests, "get", return_value=users_info or _users_info()), \
         patch.object(mod, "find_user_by_slack_identity", return_value=returning_user), \
         patch.object(mod, "join_workspace_org", return_value=joined) as join, \
         patch("routes.slack.slack_events_helpers.get_user_id_from_slack_team", return_value=team_owner), \
         patch("routes.slack.slack_routes.complete_slack_connection", return_value=None) as connect, \
         patch.object(mod, "db_pool", db_pool), \
         patch.object(signin, "db_pool", handoff_db), \
         patch.object(signin, "mint_handoff", return_value="HANDOFF"), \
         patch("utils.auth.enforcer.assign_role_to_user", create=True), \
         patch("utils.auth.command_policy.seed_default_command_policy", create=True), \
         patch("utils.auth.tool_registry.seed_org_tool_permissions", create=True), \
         patch("routes.audit_routes.record_audit_event", create=True), \
         patch("utils.hooks.get_hook", return_value=lambda *a: None):
        yield connect, cur, join


class TestGating:
    def test_disabled_flag_blocks_start(self, slack_app, monkeypatch):
        app, mod, _ = slack_app
        monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "false")
        resp = app.test_client().get("/slack/signup/start")
        assert b"not available" in resp.data

    def test_missing_slack_credentials_blocks_start(self, slack_app, monkeypatch):
        app, mod, _ = slack_app
        monkeypatch.setattr(mod, "CLIENT_SECRET", None)
        resp = app.test_client().get("/slack/signup/start")
        assert b"not available" in resp.data


class TestStart:
    def test_redirects_to_slack_install_with_signed_state(self, slack_app):
        app, mod, _ = slack_app
        resp = app.test_client().get("/slack/signup/start")
        assert resp.location.startswith("https://slack.com/oauth/v2/authorize?client_id=cid")
        state = resp.location.split("state=", 1)[1]
        with app.test_request_context():
            assert mod.load_signup_state(state) is not None


class TestCallbackDispatch:
    def test_in_app_state_never_enters_signup(self, slack_app):
        app, mod, routes = slack_app
        with patch.object(mod, "handle_signup_callback") as signup, \
             patch.object(routes, "exchange_code_for_token", return_value=_TOKEN_DATA), \
             patch.object(routes, "complete_slack_connection", return_value=None) as connect:
            resp = app.test_client().get("/slack/callback?code=c&state=user-123")
        signup.assert_not_called()
        assert connect.call_args.args[0] == "user-123"
        assert "/slack/manage?slack_auth=success" in resp.location

    def test_github_signup_state_is_not_a_slack_signup_state(self, slack_app, monkeypatch):
        app, mod, _ = slack_app
        monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")
        from routes.github import github_signup
        with app.test_request_context():
            gh_state = github_signup._sign_signup_state()
            assert mod.load_signup_state(gh_state) is None


class TestSignupCallback:
    def test_new_installer_gets_account_slack_connection_and_login(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client, "?utm_source=aurorasre.ai&next=https://evil.example")
        # fetchone: email free, user id, org name free, slug free, org id.
        with _slack(mod, db_rows=[None, ("user-new",), None, None, ("org-new",)]) as (connect, cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert resp.status_code == 302
        assert "/sign-in?handoff=HANDOFF&via=slack&slack_auth=success" in resp.location
        assert "utm_source=aurorasre.ai" in resp.location
        assert "evil" not in resp.location
        assert connect.call_args.args == ("user-new", _TOKEN_DATA)
        insert = next(c for c in cur.execute.call_args_list if "INSERT INTO users" in c.args[0])
        assert insert.args[1][0] == "dana@acme.io"
        # Linked to the Slack identity, so "Sign in with Slack" finds it later.
        assert insert.args[1][-2:] == ("T1", "U1")

    def test_returning_installer_logs_in_without_reconnecting(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, returning_user="user-old", team_owner="user-old") as (connect, cur, join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "/sign-in?handoff=HANDOFF&via=slack&slack_auth=already" in resp.location
        connect.assert_not_called()
        join.assert_not_called()
        cur.execute.assert_not_called()

    def test_returning_installer_with_disconnected_workspace_is_reconnected(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, returning_user="user-old", team_owner=None) as (connect, cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "slack_auth=success" in resp.location
        assert connect.call_args.args[0] == "user-old"
        cur.execute.assert_not_called()

    def test_workspace_owned_by_another_org_joins_it_instead(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, joined="user-joined") as (connect, cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "/sign-in?handoff=HANDOFF&via=slack_join" in resp.location
        connect.assert_not_called()
        cur.execute.assert_not_called()

    def test_refused_join_is_passed_through_without_provisioning(self, slack_app):
        app, mod, _ = slack_app
        from flask import redirect
        client = app.test_client()
        state = _start_state(client)
        with app.test_request_context():
            refusal = redirect("/sign-in?error=slack_not_member")
        with _slack(mod, joined=refusal) as (connect, cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "error=slack_not_member" in resp.location
        connect.assert_not_called()
        cur.execute.assert_not_called()

    def test_email_match_alone_never_logs_in(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, db_rows=[("existing-user",)]) as (connect, _cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "/sign-in?error=slack_account_exists" in resp.location
        assert "handoff" not in resp.location
        connect.assert_not_called()

    @pytest.mark.parametrize("users_info", [
        _users_info(team_id="T-OTHER"),
        _users_info(email=None),
        _users_info(is_bot=True),
    ])
    def test_unverifiable_identity_provisions_nothing(self, slack_app, users_info):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, users_info=users_info) as (connect, cur, _join):
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert b"Could not verify your Slack identity" in resp.data
        connect.assert_not_called()
        cur.execute.assert_not_called()

    def test_slack_setup_failure_still_logs_new_user_in(self, slack_app):
        app, mod, _ = slack_app
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod, db_rows=[None, ("user-new",), None, None, ("org-new",)]) as (connect, _cur, _join):
            connect.return_value = "storage_failed"
            resp = client.get(f"/slack/callback?code=c&state={state}")
        assert "/sign-in?handoff=HANDOFF&via=slack&slack_auth=failed" in resp.location


class TestCancel:
    def test_cancel_returns_to_the_website(self, slack_app, monkeypatch):
        app, mod, _ = slack_app
        monkeypatch.setattr(mod, "MARKETING_SITE_URL", "https://www.example-site.test")
        client = app.test_client()
        state = _start_state(client)
        with _slack(mod) as (connect, cur, _join):
            resp = client.get(f"/slack/callback?error=access_denied&state={state}")
        assert resp.location == "https://www.example-site.test/?slack=cancelled#add-to-slack"
        connect.assert_not_called()
        cur.execute.assert_not_called()

    def test_cancel_without_a_website_goes_to_sign_in(self, slack_app, monkeypatch):
        app, mod, _ = slack_app
        monkeypatch.setattr(mod, "MARKETING_SITE_URL", "")
        client = app.test_client()
        state = _start_state(client)
        resp = client.get(f"/slack/callback?error=access_denied&state={state}")
        assert resp.location.endswith("/sign-in?error=slack_cancelled")
