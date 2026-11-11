"""Tests for the GitHub App repo auto-import, the hourly sync and the backfill.

``import_installation_repos`` (routes/github/github_repo_metadata.py) UPSERTs
an installation's repos into ``connected_repos``. These tests run it against
a small in-memory stand-in for the tables it touches, so they check what ends
up connected and enrolled rather than which SQL was sent.

GitHub API and Celery ``delay`` are mocked — no I/O.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import routes.github.github_repo_metadata as mod

_USER = "user-1"
_ORG = "org-1"
_INSTALL = 4242


class _MaxRetriesExceeded(Exception):
    """Stand-in for Celery's ``self.MaxRetriesExceededError`` exception class."""


def _task_self():
    """Fake bound-task ``self`` so the body runs without Celery (stubbed in CI)."""
    return SimpleNamespace(retry=MagicMock(), MaxRetriesExceededError=_MaxRetriesExceeded)


class _FakeDb:
    """The rows the importer reads and writes, keyed the way the SQL keys them."""

    def __init__(self, connected=None, imported_at=None, excluded=()):
        # {repo_full_name: {"owner": user_id, "gating": bool}}
        self.connected = {
            name: {"owner": owner, "gating": gating}
            for name, (owner, gating) in (connected or {}).items()
        }
        self.imported_at = imported_at
        self.excluded = set(excluded)
        self.commits = 0
        self._result = []

    # cursor API
    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._result = []
        if "MAX(repos_imported_at)" in s:
            self._result = [(self.imported_at,)]
        elif s.startswith("SELECT repo_full_name FROM github_repo_exclusions"):
            self._result = [(n,) for n in params[1] if n in self.excluded]
        elif s.startswith("DELETE FROM github_repo_exclusions"):
            self.excluded -= set(params[1])
        elif s.startswith("SELECT repo_full_name, user_id FROM connected_repos"):
            self._result = [(n, r["owner"]) for n, r in self.connected.items()]
        elif s.startswith("INSERT INTO connected_repos"):
            owner, _org, name = params[0], params[1], params[2]
            if name not in self.connected:
                self.connected[name] = {"owner": owner, "gating": params[-1]}
        elif s.startswith("UPDATE user_github_installations SET repos_imported_at"):
            if self.imported_at is None:
                self.imported_at = "now"
        elif s.startswith("UPDATE connected_repos SET change_gating_enabled = TRUE"):
            for row in self.connected.values():
                row["gating"] = True
        elif "FROM user_github_installations ugi JOIN github_installations" in s:
            self._result = [(_USER,)]
        else:
            raise AssertionError(f"unexpected SQL: {s[:80]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    rowcount = 0

    def pool(self):
        db = self

        class _Conn:
            def commit(self):
                db.commits += 1

            @contextmanager
            def cursor(self):
                yield db

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        pool = MagicMock()
        pool.get_admin_connection.side_effect = lambda: _Conn()
        return pool

    def gated(self):
        return {n for n, r in self.connected.items() if r["gating"]}


def _run(repos, db=None, org=_ORG, token_exc=None, task=None, owner=_USER, **kwargs):
    db = db or _FakeDb()
    gen = MagicMock()
    with patch("utils.auth.github_app_token.get_installation_token") as get_tok, \
         patch("routes.github.github_user_repos._fetch_installation_repos",
               return_value=repos) as fetch, \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=org), \
         patch("utils.db.connection_pool.db_pool", db.pool()), \
         patch("tasks.github_webhook_tasks.auto_enroll_owner", return_value=owner), \
         patch.object(mod, "generate_repo_metadata", gen):
        if token_exc is not None:
            get_tok.side_effect = token_exc
        else:
            get_tok.return_value = "ghs_installtoken"
        if isinstance(repos, Exception):
            fetch.side_effect = repos
        mod._import_installation_repos(task or _task_self(), _USER, _INSTALL, **kwargs)
    return db, gen, fetch


def _repo(name, created="2026-09-01T00:00:00Z"):
    return {"full_name": name, "id": 1, "default_branch": "main", "created_at": created}


class TestImportInstallationRepos:
    def test_first_import_connects_everything_and_marks_the_install(self):
        db, gen, _ = _run([_repo("acme/api"), _repo("acme/web")], enroll_change_gating=True)
        assert db.gated() == {"acme/api", "acme/web"}
        assert db.imported_at is not None
        assert {c.args for c in gen.delay.call_args_list} == {
            (_USER, "acme/api"), (_USER, "acme/web"),
        }

    def test_existing_repo_keeps_its_owner_and_review_choice(self):
        db = _FakeDb(connected={"shared/repo": ("other-user", False)})
        db, gen, _ = _run([_repo("shared/repo")], db=db, enroll_change_gating=True)
        assert db.connected["shared/repo"] == {"owner": "other-user", "gating": False}
        gen.delay.assert_not_called()

    def test_empty_install_still_counts_as_imported(self):
        db, _, _ = _run([])
        assert db.connected == {}
        assert db.imported_at is not None

    def test_suspended_or_uninstalled_installation_is_left_alone(self):
        from utils.auth.github_app_token import (
            GitHubAppInstallationNotFound,
            GitHubAppInstallationSuspended,
        )

        for exc in (GitHubAppInstallationSuspended("x"), GitHubAppInstallationNotFound("x")):
            task = _task_self()
            db, _, fetch = _run([_repo("acme/api")], token_exc=exc, task=task)
            fetch.assert_not_called()
            task.retry.assert_not_called()
            assert db.imported_at is None

    def test_no_org_aborts_before_writing(self):
        db, gen, _ = _run([_repo("acme/api")], org=None)
        assert db.connected == {} and db.commits == 0
        gen.delay.assert_not_called()


class TestImportRunsOncePerLink:
    """Logins, relinks and claims must not bring back repos removed in Aurora."""

    def test_full_import_skipped_once_one_landed(self):
        db = _FakeDb(connected={"acme/api": (_USER, True)}, imported_at=datetime(2026, 9, 1))
        db, _, fetch = _run([_repo("acme/api"), _repo("acme/removed")], db=db,
                            enroll_change_gating=True)
        fetch.assert_not_called()
        assert set(db.connected) == {"acme/api"}

    def test_force_reimports_but_skips_repos_removed_in_the_picker(self):
        db = _FakeDb(imported_at=datetime(2026, 9, 1), excluded={"acme/removed"})
        db, _, _ = _run([_repo("acme/missed"), _repo("acme/removed")], db=db,
                        enroll_change_gating=True, force=True)
        assert db.gated() == {"acme/missed"}

    def test_granting_a_removed_repo_again_on_github_brings_it_back(self):
        db = _FakeDb(imported_at=datetime(2026, 9, 1), excluded={"acme/back"})
        db, _, _ = _run([_repo("acme/back")], db=db, enroll_change_gating=True,
                        only_repos=["acme/back"])
        assert db.gated() == {"acme/back"}
        assert db.excluded == set()

    def test_review_turned_off_after_queueing_is_respected(self):
        db, _, fetch = _run([_repo("acme/new")], owner=None, enroll_change_gating=True,
                            only_repos=["acme/new"], require_enrollment=True)
        fetch.assert_not_called()
        assert db.connected == {}


class TestListingFailure:
    """A GitHub outage during the listing must not read as "no repos"."""

    def test_failed_listing_retries_and_leaves_the_install_unmarked(self):
        from routes.github.github_user_repos import InstallationRepoListError

        task = _task_self()
        db, _, fetch = _run(InstallationRepoListError("status=502"), task=task)
        assert fetch.call_args.kwargs == {"strict": True}
        task.retry.assert_called_once()
        assert db.connected == {} and db.imported_at is None

    def test_backoff_grows_and_is_capped(self):
        countdowns = []
        for attempt in (0, 3, 20):
            task = _task_self()
            task.request = SimpleNamespace(retries=attempt)
            mod._retry_import(task, _INSTALL, "repo listing")
            countdowns.append(task.retry.call_args.kwargs["countdown"])
        assert countdowns == [60, 480, mod._IMPORT_RETRY_CAP_SEC]


class TestStrictListing:
    def _list(self, pages):
        from routes.github.github_user_repos import _fetch_installation_repos

        responses = []
        for body in pages:
            r = MagicMock(status_code=200)
            r.json.return_value = body
            responses.append(r)
        with patch("routes.github.github_user_repos.requests.get", side_effect=responses):
            return _fetch_installation_repos("t", _INSTALL, strict=True)

    def test_malformed_body_raises_instead_of_reading_as_empty(self):
        from routes.github.github_user_repos import InstallationRepoListError

        for body in ({}, {"repositories": None}, []):
            try:
                self._list([body])
            except InstallationRepoListError:
                continue
            raise AssertionError(f"{body!r} read as an empty installation")

    def test_lists_past_the_lenient_page_limit(self):
        import routes.github.github_user_repos as repos_mod

        full = {"repositories": [_repo("a/b")] * repos_mod._PER_PAGE}
        pages = [full] * (repos_mod._REPOS_PAGE_LIMIT + 1) + [{"repositories": []}]
        assert len(self._list(pages)) == repos_mod._PER_PAGE * (repos_mod._REPOS_PAGE_LIMIT + 1)


class TestHourlySync:
    """The safety net repairs failed imports without undoing removals."""

    def _sync(self, rows, owner=_USER):
        db = MagicMock()

        @contextmanager
        def _conn():
            conn = MagicMock()
            cur = MagicMock()
            cur.fetchall.return_value = rows
            conn.cursor.return_value.__enter__.return_value = cur
            conn.__enter__.return_value = conn
            yield conn

        db.get_admin_connection.side_effect = lambda: _conn().__enter__()
        task = MagicMock()
        with patch("utils.db.connection_pool.db_pool", db), \
             patch("tasks.github_webhook_tasks.auto_enroll_owner", return_value=owner), \
             patch.object(mod, "import_installation_repos", task):
            mod._sync_installation_repos()
        return task

    def test_install_whose_first_import_never_landed_gets_it(self):
        task = self._sync([(_INSTALL, None)])
        assert task.delay.call_args.kwargs["created_after"] is None

    def test_imported_install_only_gets_repos_created_since(self):
        task = self._sync([(_INSTALL, datetime(2026, 9, 27, 0, 4))])
        assert task.delay.call_args.kwargs["created_after"] == "2026-09-27T00:04:00Z"

    def test_repo_existing_at_import_and_now_missing_is_not_readded(self):
        # End to end through the importer: removed on purpose stays removed.
        db = _FakeDb(connected={"acme/kept": (_USER, True)}, imported_at=datetime(2026, 9, 27))
        db, _, _ = _run(
            [_repo("acme/kept"), _repo("acme/removed"), _repo("acme/new", "2026-10-10T00:00:00Z")],
            db=db, enroll_change_gating=True, created_after="2026-09-27T00:00:00Z",
        )
        assert set(db.connected) == {"acme/kept", "acme/new"}

    def test_install_with_review_turned_off_is_skipped(self):
        task = self._sync([(_INSTALL, None)], owner=None)
        task.delay.assert_not_called()


class TestBackfill:
    def test_turns_review_on_and_forces_a_full_import(self):
        db = _FakeDb(connected={"acme/api": (_USER, False)}, imported_at=datetime(2026, 9, 1))
        task = MagicMock()
        with patch("utils.db.connection_pool.db_pool", db.pool()), \
             patch("utils.auth.stateless_auth.set_rls_context", return_value=_ORG), \
             patch.object(mod, "import_installation_repos", task):
            mod._backfill_installation_repos([_INSTALL])
        assert db.gated() == {"acme/api"}
        assert task.delay.call_args.kwargs["force"] is True
