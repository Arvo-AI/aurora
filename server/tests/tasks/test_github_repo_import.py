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


def _run(repos, existing=(), org="org-1", token_exc=None, task=None, **kwargs):
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
        if isinstance(repos, Exception):
            fetch.side_effect = repos
        mod._import_installation_repos(task or _task_self(), _USER, _INSTALL, **kwargs)
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


class TestListingFailure:
    """A GitHub outage during the listing must not read as "no repos"."""

    def test_failed_listing_retries_and_writes_nothing(self):
        from routes.github.github_user_repos import InstallationRepoListError

        task = _task_self()
        conn, cur, gen, fetch = _run(InstallationRepoListError("status=502"), task=task)
        assert fetch.call_args.kwargs == {"strict": True}
        task.retry.assert_called_once()
        assert task.retry.call_args.kwargs["max_retries"] == mod._IMPORT_MAX_RETRIES
        assert _insert_calls(cur) == []
        conn.commit.assert_not_called()

    def test_backoff_grows_and_is_capped(self):
        countdowns = []
        for attempt in (0, 3, 20):
            task = _task_self()
            task.request = SimpleNamespace(retries=attempt)
            mod._retry_import(task, _INSTALL, "repo listing")
            countdowns.append(task.retry.call_args.kwargs["countdown"])
        assert countdowns == [60, 480, mod._IMPORT_RETRY_CAP_SEC]


class TestImportScope:
    _REPOS = [
        {"full_name": "acme/old", "created_at": "2026-09-01T00:00:00Z"},
        {"full_name": "acme/new", "created_at": "2026-10-10T12:00:00Z"},
    ]

    def test_only_repos_limits_the_import(self):
        _, cur, gen, _ = _run(self._REPOS, only_repos=["acme/new"])
        assert [c.args[1][2] for c in _insert_calls(cur)] == ["acme/new"]

    def test_created_after_skips_repos_that_existed_at_install(self):
        # Repos that existed at install and are missing were removed on
        # purpose; only ones created since come back.
        _, cur, gen, _ = _run(self._REPOS, created_after="2026-09-26T20:04:00Z")
        assert [c.args[1][2] for c in _insert_calls(cur)] == ["acme/new"]


class TestHourlySync:
    """The safety net repairs failed imports without undoing removals."""

    def _sync(self, rows, state, backfill=False):
        db_pool, _, cur = _mock_db()
        cur.fetchall.return_value = rows
        task = MagicMock()
        with patch("utils.db.connection_pool.db_pool", db_pool), \
             patch("tasks.github_webhook_tasks.enrollment_state", return_value=state), \
             patch.object(mod, "import_installation_repos", task):
            mod._sync_installation_repos(backfill=backfill)
        return task

    def test_new_install_with_nothing_connected_gets_full_import(self):
        task = self._sync([(_INSTALL, True)], (_USER, 0, None))
        task.delay.assert_called_once_with(
            _USER, _INSTALL, enroll_change_gating=True, created_after=None
        )

    def test_old_install_with_nothing_connected_is_left_alone(self):
        # Zero repos long after linking means they were all removed in Aurora.
        task = self._sync([(_INSTALL, False)], (_USER, 0, None))
        task.delay.assert_not_called()

    def test_backfill_repairs_old_empty_install(self):
        task = self._sync([(_INSTALL, False)], (_USER, 0, None), backfill=True)
        assert task.delay.call_args.kwargs["created_after"] is None

    def test_backfill_imports_repos_created_since_first_import(self):
        from datetime import datetime

        first = datetime(2026, 9, 27, 0, 4)
        task = self._sync([(_INSTALL, False)], (_USER, 55, first), backfill=True)
        assert task.delay.call_args.kwargs["created_after"] == "2026-09-27T00:04:00Z"

    def test_install_with_review_turned_off_is_skipped(self):
        task = self._sync([(_INSTALL, True)], (None, 0, None))
        task.delay.assert_not_called()
