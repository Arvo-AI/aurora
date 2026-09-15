"""Tests for the one-click GitHub App signup flow (routes/github/github_signup.py).

Pins the security contract:

- The whole flow is OFF unless ``HOSTED_SIGNUP_ENABLED=true`` AND the App is
  runtime-enabled AND a client secret exists (OSS deployments stay invite-only).
- The signup state is signed + expiring and its salt differs from the
  authenticated install flow's, so tokens are not interchangeable.
- The callback verifies state and installation BEFORE any provisioning; a
  missing/forged param renders the hard-coded error template with zero writes.
- Identity comes only from the OAuth code exchange; private-email profiles
  fall back to the noreply convention rather than failing.
- An email-only collision (GitHub email matches an Aurora account that never
  linked this GitHub identity) redirects to sign-in — never auto-login.
- The handoff exchange burns the token on first use (single redemption).

GitHub API and DB are mocked — no I/O.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def signup_app(monkeypatch):
    """Fresh Flask app with the signup blueprint, hosted mode fully enabled."""
    mods_to_evict = [
        m for m in sys.modules
        if m.startswith(("routes.github.github_signup",))
    ]
    for _mod in mods_to_evict:
        del sys.modules[_mod]

    monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "shhh")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "aurora-test")

    from flask import Flask
    from routes.github import github_signup as mod

    application = Flask(__name__)  # NOSONAR
    application.config["GITHUB_APP_ENABLED"] = True
    application.register_blueprint(mod.github_signup_bp, url_prefix="/github")

    @application.errorhandler(500)
    def _err(e):  # noqa: ANN001
        return "err", 500

    # Error template is rendered with render_template — stub it so tests
    # don't need the template file on disk.
    monkeypatch.setattr(
        mod.flask, "render_template",
        lambda *a, **kw: f"ERROR:{kw.get('error', '')}",
    )
    return application, mod


def _install_payload(install_id=777, login="octo-org", account_id=99, acct_type="Organization"):
    return {
        "id": install_id,
        "account": {"login": login, "id": account_id, "type": acct_type},
        "target_type": acct_type,
        "permissions": {"pull_requests": "write"},
        "events": ["pull_request"],
        "repository_selection": "selected",
        "suspended_at": None,
    }


class TestGating:
    def test_disabled_flag_blocks_start(self, signup_app, monkeypatch):
        app, mod = signup_app
        monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "false")
        resp = app.test_client().get("/github/app/signup/start")
        assert resp.status_code == 200
        assert b"not available" in resp.data

    def test_missing_client_secret_blocks(self, signup_app, monkeypatch):
        app, mod = signup_app
        monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET")
        resp = app.test_client().get("/github/app/signup/start")
        assert b"not available" in resp.data

    def test_app_disabled_blocks(self, signup_app):
        app, mod = signup_app
        app.config["GITHUB_APP_ENABLED"] = False
        resp = app.test_client().get("/github/app/signup/start")
        assert b"not available" in resp.data

    def test_callback_gated_too(self, signup_app, monkeypatch):
        app, mod = signup_app
        monkeypatch.setenv("HOSTED_SIGNUP_ENABLED", "false")
        resp = app.test_client().get(
            "/github/app/signup/callback?installation_id=1&state=x&code=y"
        )
        assert b"not available" in resp.data


class TestStartRedirect:
    def test_redirects_to_github_with_signed_state(self, signup_app):
        app, mod = signup_app
        resp = app.test_client().get("/github/app/signup/start")
        assert resp.status_code == 302
        assert resp.location.startswith(
            "https://github.com/apps/aurora-test/installations/new?state="
        )
        state = resp.location.split("state=", 1)[1]
        with app.test_request_context():
            assert mod._verify_signup_state(state) is True


class TestStateToken:
    def test_round_trip(self, signup_app):
        app, mod = signup_app
        with app.test_request_context():
            assert mod._verify_signup_state(mod._sign_signup_state()) is True

    def test_garbage_rejected(self, signup_app):
        app, mod = signup_app
        with app.test_request_context():
            assert mod._verify_signup_state("not-a-token") is False

    def test_install_flow_state_not_interchangeable(self, signup_app):
        # A state signed for the AUTHENTICATED install flow (different salt)
        # must not validate for signup — the flows have different trust levels.
        app, mod = signup_app
        from itsdangerous import URLSafeTimedSerializer

        other = URLSafeTimedSerializer(
            "test-secret", salt="aurora.github.app.install-state.v1"
        ).dumps({"flow": "signup"})
        with app.test_request_context():
            assert mod._verify_signup_state(other) is False


class TestCallbackValidation:
    @pytest.mark.parametrize("qs", [
        "installation_id=1&code=c",               # no state
        "state=s&code=c",                         # no installation_id
    ])
    def test_missing_params_rejected(self, signup_app, qs):
        app, mod = signup_app
        resp = app.test_client().get(f"/github/app/signup/callback?{qs}")
        assert b"Missing required parameters" in resp.data

    def test_signup_state_without_code_rejected(self, signup_app):
        # A valid signup state but no OAuth code means the App is missing
        # "Request user authorization during installation" — fail loudly
        # rather than provisioning an account with no identity.
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        with patch.object(mod, "_verify_installation") as verify:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=1&state={state}"
            )
        assert b"Missing required parameters" in resp.data
        verify.assert_not_called()

    def test_install_flow_state_delegates_to_authenticated_callback(self, signup_app):
        # With OAuth-during-install on, GitHub sends in-app installs (state
        # signed under the INSTALL salt, bound to a logged-in user) to this
        # same first Callback URL. They must reach the authenticated handler
        # untouched — never the signup provisioning path.
        app, mod = signup_app
        from itsdangerous import URLSafeTimedSerializer

        install_state = URLSafeTimedSerializer(
            "test-secret", salt="aurora.github.app.install-state.v1"
        ).dumps("user-123")
        with patch("routes.github.github_app.github_app_install_callback",
                   return_value="DELEGATED") as delegate, \
             patch.object(mod, "_verify_installation") as verify, \
             patch.object(mod, "_provision_and_handoff") as provision:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=1&state={install_state}&code=c"
            )
        assert resp.data == b"DELEGATED"
        delegate.assert_called_once_with()
        verify.assert_not_called()
        provision.assert_not_called()

    def test_forged_state_is_not_delegated(self, signup_app):
        app, mod = signup_app
        with patch("routes.github.github_app.github_app_install_callback") as delegate:
            resp = app.test_client().get(
                "/github/app/signup/callback?installation_id=1&state=forged&code=c"
            )
        assert b"could not be verified" in resp.data
        delegate.assert_not_called()

    def test_bad_state_rejected_before_github_call(self, signup_app):
        app, mod = signup_app
        with patch.object(mod, "_verify_installation") as verify:
            resp = app.test_client().get(
                "/github/app/signup/callback?installation_id=1&state=forged&code=c"
            )
        assert b"could not be verified" in resp.data
        verify.assert_not_called()

    @pytest.mark.parametrize("bad_id", ["abc", "-5", "0"])
    def test_bad_installation_id_rejected(self, signup_app, bad_id):
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        resp = app.test_client().get(
            f"/github/app/signup/callback?installation_id={bad_id}&state={state}&code=c"
        )
        assert b"installation could not be verified" in resp.data

    def test_unverifiable_installation_writes_nothing(self, signup_app):
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        with patch.object(mod, "_verify_installation", return_value=None), \
             patch.object(mod, "_provision_and_handoff") as provision:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=777&state={state}&code=c"
            )
        assert b"Could not verify installation" in resp.data
        provision.assert_not_called()

    def test_installation_id_mismatch_rejected(self, signup_app):
        # GitHub returning metadata for a DIFFERENT id than the query param
        # (or a tampered proxy) must not provision.
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        with patch.object(
            mod, "_verify_installation", return_value=_install_payload(install_id=888)
        ), patch.object(mod, "_provision_and_handoff") as provision:
            app.test_client().get(
                f"/github/app/signup/callback?installation_id=777&state={state}&code=c"
            )
        provision.assert_not_called()

    def test_failed_identity_exchange_writes_nothing(self, signup_app):
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        with patch.object(
            mod, "_verify_installation", return_value=_install_payload()
        ), patch.object(mod, "_exchange_code_for_identity", return_value=None), \
             patch.object(mod, "_provision_and_handoff") as provision:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=777&state={state}&code=c"
            )
        assert b"identity" in resp.data
        provision.assert_not_called()

    _IDENTITY = {"id": 42, "login": "octocat", "name": "Octo Cat", "email": "octo@cat.dev"}

    def test_unauthorized_installation_writes_nothing(self, signup_app):
        # Invariant #6: the OAuth user swapped in someone else's (real)
        # installation id. Must reject with ZERO DB writes and no import.
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        db_pool, conn, cur = _mock_db(mod, [])
        with patch.object(mod, "_verify_installation", return_value=_install_payload()), \
             patch.object(mod, "_exchange_code_for_identity",
                          return_value=(self._IDENTITY, "gho_x")), \
             patch.object(mod, "_user_authorized_for_installation",
                          return_value=False) as authz, \
             patch.object(mod, "db_pool", db_pool), \
             patch("routes.github.github_repo_metadata.import_installation_repos") as task:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=777&state={state}&code=c"
            )
        assert b"does not have access to this installation" in resp.data
        authz.assert_called_once_with("gho_x", 777)
        cur.execute.assert_not_called()
        conn.commit.assert_not_called()
        task.delay.assert_not_called()

    def test_authorized_installation_provisions_and_imports(self, signup_app):
        app, mod = signup_app
        with app.test_request_context():
            state = mod._sign_signup_state()
        with patch.object(mod, "_verify_installation", return_value=_install_payload()), \
             patch.object(mod, "_exchange_code_for_identity",
                          return_value=(self._IDENTITY, "gho_x")), \
             patch.object(mod, "_user_authorized_for_installation", return_value=True), \
             patch.object(mod, "_provision_and_handoff",
                          return_value=(mod.flask.redirect("/x"), "user-1", 777, True)), \
             patch("routes.github.github_repo_metadata.import_installation_repos") as task:
            resp = app.test_client().get(
                f"/github/app/signup/callback?installation_id=777&state={state}&code=c"
            )
        assert resp.status_code == 302
        task.delay.assert_called_once_with("user-1", 777, enroll_change_gating=True)


class TestIdentityExchange:
    def _token_resp(self, token="gho_x"):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"access_token": token}
        return r

    def _user_resp(self, **overrides):
        r = MagicMock()
        r.status_code = 200
        payload = {"id": 42, "login": "octocat", "name": "Octo Cat", "email": "octo@cat.dev"}
        payload.update(overrides)
        r.json.return_value = payload
        return r

    def test_happy_path(self, signup_app):
        app, mod = signup_app
        with app.test_request_context(), \
             patch.object(mod.requests, "post", return_value=self._token_resp()), \
             patch.object(mod.requests, "get", return_value=self._user_resp()), \
             patch("connectors.github_connector.config.load_github_app_config",
                   return_value=MagicMock(client_id="Iv1.test")):
            identity, token = mod._exchange_code_for_identity("c0de")
        assert identity == {"id": 42, "login": "octocat", "name": "Octo Cat", "email": "octo@cat.dev"}
        assert token == "gho_x"

    def test_private_email_uses_verified_primary(self, signup_app):
        app, mod = signup_app
        emails = MagicMock()
        emails.status_code = 200
        emails.json.return_value = [
            {"email": "old@x.dev", "primary": False, "verified": True},
            {"email": "real@x.dev", "primary": True, "verified": True},
        ]
        with app.test_request_context(), \
             patch.object(mod.requests, "post", return_value=self._token_resp()), \
             patch.object(mod.requests, "get",
                          side_effect=[self._user_resp(email=None), emails]), \
             patch("connectors.github_connector.config.load_github_app_config",
                   return_value=MagicMock(client_id="Iv1.test")):
            identity, _ = mod._exchange_code_for_identity("c0de")
        assert identity["email"] == "real@x.dev"

    def test_private_email_falls_back_to_noreply(self, signup_app):
        app, mod = signup_app
        emails_404 = MagicMock()
        emails_404.status_code = 404
        with app.test_request_context(), \
             patch.object(mod.requests, "post", return_value=self._token_resp()), \
             patch.object(mod.requests, "get",
                          side_effect=[self._user_resp(email=None), emails_404]), \
             patch("connectors.github_connector.config.load_github_app_config",
                   return_value=MagicMock(client_id="Iv1.test")):
            identity, _ = mod._exchange_code_for_identity("c0de")
        assert identity["email"] == "42+octocat@users.noreply.github.com"

    def test_no_access_token_returns_none(self, signup_app):
        app, mod = signup_app
        bad = MagicMock()
        bad.status_code = 200
        bad.json.return_value = {"error": "bad_verification_code"}
        with app.test_request_context(), \
             patch.object(mod.requests, "post", return_value=bad), \
             patch("connectors.github_connector.config.load_github_app_config",
                   return_value=MagicMock(client_id="Iv1.test")):
            assert mod._exchange_code_for_identity("c0de") is None


class TestInstallationAuthorization:
    def _page(self, ids, status=200):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = {
            "total_count": len(ids),
            "installations": [{"id": i} for i in ids],
        }
        return r

    def test_listed_installation_is_authorized(self, signup_app):
        app, mod = signup_app
        with patch.object(mod.requests, "get", return_value=self._page([5, 777])) as get:
            assert mod._user_authorized_for_installation("gho_x", 777) is True
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer gho_x"

    def test_unlisted_installation_is_rejected(self, signup_app):
        app, mod = signup_app
        with patch.object(mod.requests, "get", return_value=self._page([5, 6])):
            assert mod._user_authorized_for_installation("gho_x", 777) is False

    def test_api_error_fails_closed(self, signup_app):
        app, mod = signup_app
        with patch.object(mod.requests, "get", return_value=self._page([], status=401)):
            assert mod._user_authorized_for_installation("gho_x", 777) is False

    def test_network_error_fails_closed(self, signup_app):
        app, mod = signup_app
        with patch.object(mod.requests, "get",
                          side_effect=mod.requests.RequestException("boom")):
            assert mod._user_authorized_for_installation("gho_x", 777) is False

    def test_paginates_full_pages(self, signup_app):
        app, mod = signup_app
        full = self._page(list(range(1, 101)))          # 100 ids, none match
        with patch.object(mod.requests, "get",
                          side_effect=[full, self._page([777])]) as get:
            assert mod._user_authorized_for_installation("gho_x", 777) is True
        assert [c.kwargs["params"]["page"] for c in get.call_args_list] == [1, 2]

    def test_pagination_is_bounded(self, signup_app):
        app, mod = signup_app
        full = self._page(list(range(1, 101)))
        with patch.object(mod.requests, "get", return_value=full) as get:
            assert mod._user_authorized_for_installation("gho_x", 777) is False
        assert get.call_count == mod._USER_INSTALLATIONS_MAX_PAGES


def _mock_db(mod, fetchone_seq):
    """Patch db_pool with a cursor whose fetchone returns the given sequence."""
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_seq)
    conn = MagicMock()

    @contextmanager
    def _conn_ctx():
        yield conn

    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    db_pool = MagicMock()
    db_pool.get_admin_connection.return_value = conn
    return db_pool, conn, cur


class TestProvisioning:
    _IDENTITY = {"id": 42, "login": "octocat", "name": "Octo Cat", "email": "octo@cat.dev"}

    def test_email_collision_redirects_to_signin(self, signup_app):
        app, mod = signup_app
        # fetchone: no github_user_id match, then email EXISTS.
        db_pool, conn, cur = _mock_db(mod, [None, ("existing-user",)])
        with app.test_request_context(), patch.object(mod, "db_pool", db_pool):
            response, user_id, install_id, created = mod._provision_and_handoff(
                self._IDENTITY, _install_payload()
            )
        assert user_id is None
        assert created is False
        assert response.status_code == 302
        assert "error=account_exists" in response.location
        # Nothing committed on this path.
        conn.commit.assert_not_called()

    def test_existing_github_identity_logs_in_without_insert(self, signup_app):
        app, mod = signup_app
        # fetchone: github_user_id match -> user row; org lookup; (link upsert
        # does no fetchone); handoff mint does no fetchone.
        db_pool, conn, cur = _mock_db(mod, [("user-9",), ("org-9",)])
        with app.test_request_context(), patch.object(mod, "db_pool", db_pool), \
             patch("utils.auth.tool_registry.seed_org_tool_permissions"):
            response, user_id, install_id, created = mod._provision_and_handoff(
                self._IDENTITY, _install_payload()
            )
        assert user_id == "user-9"
        assert created is False
        assert install_id == 777
        assert response.status_code == 302
        assert "/sign-in?handoff=" in response.location
        inserts = [
            c.args[0] for c in cur.execute.call_args_list
            if "INSERT INTO users" in c.args[0]
        ]
        assert inserts == []

    def test_new_user_creates_org_and_handoff(self, signup_app):
        app, mod = signup_app
        # fetchone seq: no gh match; no email match; INSERT users
        # RETURNING; org-name check (None); slug check (None); INSERT org
        # RETURNING.
        db_pool, conn, cur = _mock_db(
            mod, [None, None, ("new-user",), None, None, ("new-org",)]
        )
        with app.test_request_context(), patch.object(mod, "db_pool", db_pool), \
             patch("utils.auth.enforcer.assign_role_to_user") as role, \
             patch("utils.auth.command_policy.seed_default_command_policy"), \
             patch("utils.auth.tool_registry.seed_org_tool_permissions"), \
             patch("routes.audit_routes.record_audit_event"):
            response, user_id, install_id, created = mod._provision_and_handoff(
                self._IDENTITY, _install_payload()
            )
        assert user_id == "new-user"
        assert created is True
        assert "/sign-in?handoff=" in response.location
        role.assert_called_once_with("new-user", "admin", "new-org")
        conn.commit.assert_called_once()
        sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
        assert "signup_handoff_hash" in sql        # handoff minted
        assert "github_installations" in sql       # install linked
        assert "email_verified" in sql             # provisioned verified

    def test_new_user_has_unusable_password(self, signup_app):
        app, mod = signup_app
        # Same fetchone order as test_new_user_creates_org_and_handoff:
        # gh-id miss, email miss, INSERT users RETURNING, org-name check,
        # slug check, INSERT org RETURNING.
        db_pool, conn, cur = _mock_db(
            mod, [None, None, ("new-user",), None, None, ("new-org",)]
        )
        with app.test_request_context(), patch.object(mod, "db_pool", db_pool), \
             patch("utils.auth.enforcer.assign_role_to_user"), \
             patch("utils.auth.command_policy.seed_default_command_policy"), \
             patch("utils.auth.tool_registry.seed_org_tool_permissions"), \
             patch("routes.audit_routes.record_audit_event"):
            mod._provision_and_handoff(self._IDENTITY, _install_payload())
        insert = next(
            c for c in cur.execute.call_args_list
            if "INSERT INTO users" in c.args[0]
        )
        password_hash = insert.args[1][1]
        # bcrypt of random bytes — never equal to any typable password's hash,
        # and never empty (which some checkpw impls short-circuit on).
        assert password_hash.startswith("$2")


class TestOrgIdentity:
    def test_collision_suffixes(self, signup_app):
        app, mod = signup_app
        cur = MagicMock()
        cur.fetchone.side_effect = [("taken",)]  # name collision
        name, slug = mod._unique_org_identity(cur, "Acme")
        assert name.startswith("Acme-")
        assert slug.startswith("acme-")

    def test_no_collision_passthrough(self, signup_app):
        app, mod = signup_app
        cur = MagicMock()
        cur.fetchone.side_effect = [None, None]
        name, slug = mod._unique_org_identity(cur, "Acme Corp")
        assert name == "Acme Corp"
        assert slug == "acme-corp"
