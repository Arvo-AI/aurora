"""Tests for the GitHub App repo auto-import task.

Pins ``import_installation_repos`` (routes/github/github_repo_metadata.py):
after an App install, the repos the user granted on GitHub are UPSERTed into
``connected_repos`` (with ``installation_id`` set) so they don't have to be
re-selected in Aurora, metadata generation fires only for NEW rows, and the
task degrades safely (suspended install / no repos / no org → no-op).

GitHub API, DB and Celery ``delay`` are all mocked — no I/O.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import routes.github.github_repo_metadata as mod

_USER = "user-1"
_INSTALL = 4242


class _MaxRetriesExceeded(Exception):
    """Stand-in for Celery's ``self.MaxRetriesExceededError`` exception class."""


def _task_self():
    """Fake bound-task ``self`` so the body runs without Celery (stubbed in CI).

    Tests call ``_import_installation_repos`` directly rather than the
    ``@celery_app.task``-decorated wrapper, because the lightweight test env
    stubs Celery and a decorated task degrades to a no-op MagicMock.
    """
    return SimpleNamespace(retry=MagicMock(), MaxRetriesExceededError=_MaxRetriesExceeded)


def _mock_db(existing_repos=()):
    """Build a db_pool whose cursor returns ``existing_repos`` for the SELECT."""
    cur = MagicMock()
    # The SELECT returns (repo_full_name, owner_user_id). Accept either a dict
    # {repo: owner} or a sequence of names (owner defaults to the installing user).
    if isinstance(existing_repos, dict):
        rows = [(name, owner) for name, owner in existing_repos.items()]
    else:
        rows = [(name, _USER) for name in existing_repos]
    cur.fetchall.return_value = rows
    conn = MagicMock()

    @contextmanager
    def _cursor_cm():
        yield cur

    conn.cursor.side_effect = _cursor_cm

    @contextmanager
    def _conn_cm():
        yield conn

    db_pool = MagicMock()
    db_pool.get_admin_connection.side_effect = _conn_cm
    return db_pool, conn, cur


def _run(repos, existing=(), org="org-1", token_exc=None):
    db_pool, conn, cur = _mock_db(existing)
    gen = MagicMock()
    with patch("utils.auth.github_app_token.get_installation_token") as get_tok, \
         patch("routes.github.github_user_repos._fetch_installation_repos",
               return_value=repos) as fetch, \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=org), \
         patch("utils.db.connection_pool.db_pool", db_pool), \
         patch.object(mod, "generate_repo_metadata", gen):
        if token_exc is not None:
            get_tok.side_effect = token_exc
        else:
            get_tok.return_value = "ghs_installtoken"
        mod._import_installation_repos(_task_self(), _USER, _INSTALL)
    return conn, cur, gen, fetch


def _insert_calls(cur):
    return [c for c in cur.execute.call_args_list if "INSERT INTO connected_repos" in c.args[0]]


class TestImportInstallationRepos:
    def test_imports_and_dispatches_metadata_for_new_repos(self):
        repos = [
            {"full_name": "acme/api", "id": 1, "default_branch": "main", "private": True},
            {"full_name": "acme/web", "id": 2, "default_branch": "trunk", "private": False},
        ]
        conn, cur, gen, _ = _run(repos, existing=())

        inserts = _insert_calls(cur)
        assert len(inserts) == 2
        # INSERT values tuple: (user_id, org_id, full_name, id, default_branch,
        # is_private, installation_id, repo_data) — installation_id at index 6.
        for call in inserts:
            params = call.args[1]
            assert params[0] == _USER
            assert params[6] == _INSTALL
        conn.commit.assert_called_once()
        dispatched = {c.args for c in gen.delay.call_args_list}
        assert dispatched == {(_USER, "acme/api"), (_USER, "acme/web")}

    def test_existing_repo_upserted_but_no_duplicate_metadata(self):
        repos = [
            {"full_name": "acme/api", "id": 1, "default_branch": "main"},
            {"full_name": "acme/web", "id": 2, "default_branch": "main"},
        ]
        _, cur, gen, _ = _run(repos, existing=("acme/api",))
        # Both repos are still upserted (idempotent)...
        assert len(_insert_calls(cur)) == 2
        # ...but metadata only fires for the genuinely new one.
        assert {c.args for c in gen.delay.call_args_list} == {(_USER, "acme/web")}

    def test_repo_owned_by_other_org_member_keeps_owner_no_duplicate_row(self):
        # A repo already connected by another org member must UPSERT under that
        # owner (matching the UNIQUE key) — not create a second row for it.
        repos = [{"full_name": "shared/repo", "id": 5, "default_branch": "main"}]
        _, cur, gen, _ = _run(repos, existing={"shared/repo": "other-user"})
        inserts = _insert_calls(cur)
        assert len(inserts) == 1
        assert inserts[0].args[1][0] == "other-user"  # owner preserved, no dup row
        gen.delay.assert_not_called()  # already present → no metadata re-dispatch

    def test_repo_missing_full_name_skipped(self):
        repos = [{"id": 9, "default_branch": "main"}, {"full_name": "acme/ok", "id": 1}]
        _, cur, gen, _ = _run(repos, existing=())
        assert len(_insert_calls(cur)) == 1
        assert {c.args for c in gen.delay.call_args_list} == {(_USER, "acme/ok")}

    def test_suspended_installation_is_a_noop(self):
        from utils.auth.github_app_token import GitHubAppInstallationSuspended

        _, cur, gen, fetch = _run(
            [{"full_name": "acme/api"}], token_exc=GitHubAppInstallationSuspended("x")
        )
        fetch.assert_not_called()
        cur.execute.assert_not_called()
        gen.delay.assert_not_called()

    def test_no_repos_is_a_noop(self):
        conn, cur, gen, _ = _run([], existing=())
        cur.execute.assert_not_called()
        gen.delay.assert_not_called()
        conn.commit.assert_not_called()

    def test_no_org_aborts_before_writing(self):
        # set_rls_context returning falsy (no org) must abort before any write.
        conn, cur, gen, _ = _run([{"full_name": "acme/api"}], org=None)
        assert _insert_calls(cur) == []
        conn.commit.assert_not_called()
        gen.delay.assert_not_called()
