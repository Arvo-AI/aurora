"""POST /github/app/claim-pending: finishing a one-click install after sign-in.

The installer's GitHub email matched an existing account, so the signup
callback sent them to sign in with a claim token instead of logging them in.
The claim links the installation only to the account that owns that email.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_INSTALL = {
    "id": 777,
    "account": {"login": "octo-org", "id": 99, "type": "Organization"},
    "repository_selection": "all",
}
_IDENTITY = {"id": 42, "login": "octocat", "name": "Octo", "email": "octo@cat.dev"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    from routes.github import github_app, github_signup

    app = Flask(__name__)  # NOSONAR
    app.config["GITHUB_APP_ENABLED"] = True
    with app.test_request_context():
        token = github_signup._sign_claim_token(777, _IDENTITY)
    return app, github_app, token


def _db(fetchone_seq):
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_seq)
    conn = MagicMock()

    @contextmanager
    def _cur():
        yield cur

    conn.cursor.side_effect = _cur

    @contextmanager
    def _conn():
        yield conn

    pool = MagicMock()
    pool.get_admin_connection.side_effect = _conn
    return pool, conn, cur


def _claim(env, token, fetchone_seq):
    app, mod, _ = env
    pool, conn, cur = _db(fetchone_seq)
    with app.test_request_context(json={"token": token}), \
         patch.object(mod, "db_pool", pool), \
         patch.object(mod, "get_org_id_for_user", return_value="org-1"), \
         patch.object(mod, "_orgs_owning_install", return_value=set()), \
         patch("routes.github.github_signup._verify_installation", return_value=_INSTALL), \
         patch("routes.github.github_repo_metadata.import_installation_repos") as task, \
         patch("utils.auth.tool_registry.seed_org_tool_permissions"):
        resp, status = _call(mod, "user-1")
    return resp, status, conn, task


def _call(mod, user_id):
    out = mod.github_app_claim_pending.__wrapped__(user_id)
    return out if isinstance(out, tuple) else (out, 200)


def test_owner_of_the_email_gets_the_install_and_enrolled_repos(env):
    # users row: same email, no GitHub identity yet; GitHub id unused elsewhere.
    resp, status, conn, task = _claim(env, env[2], [("octo@cat.dev", None), None])
    assert status == 200
    conn.commit.assert_called_once()
    task.delay.assert_called_once_with("user-1", 777, enroll_change_gating=True)


def test_different_account_is_refused(env):
    resp, status, conn, task = _claim(env, env[2], [("someone@else.dev", None)])
    assert status == 403
    conn.commit.assert_not_called()
    task.delay.assert_not_called()


def test_tampered_token_is_refused(env):
    resp, status, conn, task = _claim(env, env[2] + "x", [])
    assert status == 400
    task.delay.assert_not_called()


@pytest.mark.parametrize("owners, expected", [(set(), 403), ({"org-2"}, 403)])
def test_claim_of_an_install_the_org_never_linked_is_refused(env, owners, expected):
    # Claim joins an install a teammate linked. An install id nobody in the
    # org linked proves nothing about the caller, and claiming it would
    # import every repo it covers.
    app, mod, _ = env
    pool, conn, _cur = _db([])
    with app.test_request_context(), \
         patch.object(mod, "db_pool", pool), \
         patch.object(mod, "get_org_id_for_user", return_value="org-1"), \
         patch.object(mod, "_orgs_owning_install", return_value=owners), \
         patch.object(mod, "mint_app_jwt") as jwt, \
         patch("routes.github.github_repo_metadata.import_installation_repos") as task:
        out = mod.github_app_claim_installation.__wrapped__("user-1", 777)
    assert out[1] == expected
    jwt.assert_not_called()
    conn.commit.assert_not_called()
    task.delay.assert_not_called()


def test_signed_in_user_links_a_new_install_without_taking_its_github_identity(env):
    # The connect-to-my-org choice links the installation only. Attaching
    # the GitHub id would make a forwarded install link a way to log in as
    # this account.
    app, mod, _ = env
    from routes.github import github_signup

    with app.test_request_context():
        token = github_signup._sign_pending_signup(777, _IDENTITY, {})
    pool, conn, cur = _db([])
    with app.test_request_context(json={"token": token}), \
         patch.object(mod, "db_pool", pool), \
         patch.object(mod, "get_org_id_for_user", return_value="org-1"), \
         patch.object(mod, "_orgs_owning_install", return_value=set()), \
         patch("routes.github.github_signup._verify_installation", return_value=_INSTALL), \
         patch("routes.github.github_repo_metadata.import_installation_repos") as task, \
         patch("utils.auth.tool_registry.seed_org_tool_permissions"):
        out = mod.github_app_link_pending.__wrapped__("user-1")
    status = out[1] if isinstance(out, tuple) else 200
    assert status == 200
    sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
    assert "user_github_installations" in sql
    assert "github_user_id" not in sql
    task.delay.assert_called_once_with("user-1", 777, enroll_change_gating=True)


def test_link_pending_refuses_an_install_another_org_has(env):
    app, mod, _ = env
    from routes.github import github_signup

    with app.test_request_context():
        token = github_signup._sign_pending_signup(777, _IDENTITY, {})
    pool, conn, _cur = _db([])
    with app.test_request_context(json={"token": token}), \
         patch.object(mod, "db_pool", pool), \
         patch.object(mod, "get_org_id_for_user", return_value="org-1"), \
         patch.object(mod, "_orgs_owning_install", return_value={"org-2"}), \
         patch("routes.github.github_repo_metadata.import_installation_repos") as task:
        out = mod.github_app_link_pending.__wrapped__("user-1")
    assert out[1] == 403
    conn.commit.assert_not_called()
    task.delay.assert_not_called()
