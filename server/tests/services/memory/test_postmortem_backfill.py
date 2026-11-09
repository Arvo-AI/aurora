"""Backfill of placeholder postmortem descriptions (fake cursor, no DB)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from services.memory import postmortem_backfill as pb

OLD = "Postmortem for incident: DB down"
RC = "## Root Cause\nThe connection pool was exhausted by a leaked transaction in the billing worker.\n\n## Timeline\nx"


class FakeDB:
    """artifacts keyed by org; RLS filters to the current org like the real table."""

    def __init__(self, orgs):
        self.orgs = orgs  # org_id -> list of [id, category, description, content]
        self.current_org = None
        self.log = []
        self.fail_orgs = set()
        self.commits = 0
        self.rows = []
        self.rowcount = 0

    # connection API
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.log.append(("rollback", self.current_org))

    # cursor API
    def execute(self, sql, params=()):
        if "FROM users" in sql:
            self.rows = [(o, f"user-{o}") for o in self.orgs]
        elif sql.lstrip().startswith("SELECT id, description"):
            last, _, limit = params
            if self.current_org in self.fail_orgs:
                raise RuntimeError("boom")
            self.rows = [
                (r[0], r[2], r[3]) for r in sorted(self.orgs[self.current_org], key=lambda r: r[0])
                if r[1] == "postmortem" and r[2] and r[2].startswith(pb.OLD_PREFIX)
                and (last is None or r[0] > last)
            ][:limit]
        elif sql.lstrip().startswith("UPDATE artifacts"):
            new, art_id, old = params
            self.rowcount = 0
            for r in self.orgs[self.current_org]:
                if r[0] == art_id and r[2] == old:
                    r[2] = new
                    self.rowcount = 1
            self.log.append(("update", self.current_org, art_id))

    def fetchall(self):
        return self.rows


def _run(db):
    @contextmanager
    def conn_ctx():
        yield db

    def fake_rls(cursor, conn, user_id, *, org_id=None, log_prefix=""):
        db.current_org = org_id
        db.log.append(("rls", org_id))
        return org_id

    pool = MagicMock()
    pool.get_admin_connection.side_effect = conn_ctx
    with patch.object(pb, "db_pool", pool), patch.object(pb, "set_rls_context", fake_rls):
        return pb.run_backfill()


def _db():
    return FakeDB({
        "o1": [
            ["a1", "postmortem", OLD, RC],
            ["a2", "postmortem", "Postmortem for incident: No RC", "no sections here"],
            ["a3", "postmortem", "Hand-written by the user", RC],
            ["a4", "context", OLD, RC],
        ],
        "o2": [["b1", "postmortem", "Postmortem for incident: X", RC]],
    })


def test_only_old_style_postmortem_rows_updated():
    db = _db()
    stats = _run(db)
    o1 = {r[0]: r[2] for r in db.orgs["o1"]}
    assert o1["a1"].startswith("The connection pool was exhausted")
    assert o1["a2"] == "Postmortem for incident: No RC"  # no Root Cause: unchanged
    assert o1["a3"] == "Hand-written by the user"  # user-edited: untouched
    assert o1["a4"] == OLD  # other category: untouched
    assert db.orgs["o2"][0][2].startswith("The connection pool")
    assert stats["updated"] == 2 and stats["orgs"] == 2 and stats["errors"] == 0


def test_idempotent_second_run_updates_zero():
    db = _db()
    _run(db)
    before = [list(r) for rows in db.orgs.values() for r in rows]
    stats = _run(db)
    assert stats["updated"] == 0
    assert before == [list(r) for rows in db.orgs.values() for r in rows]


def test_rls_set_before_update_per_org_and_commit_per_org():
    db = _db()
    _run(db)
    for org in ("o1", "o2"):
        first_rls = db.log.index(("rls", org))
        upd = [i for i, e in enumerate(db.log) if e[0] == "update" and e[1] == org]
        assert upd and first_rls < min(upd)
    assert db.commits >= 2


def test_one_org_failure_does_not_stop_others():
    db = _db()
    db.fail_orgs = {"o1"}
    stats = _run(db)
    assert stats["errors"] == 1 and stats["updated"] == 1
    assert ("rollback", "o1") in db.log
    assert db.orgs["o2"][0][2].startswith("The connection pool")
    assert db.orgs["o1"][0][2] == OLD


def test_concurrent_edit_not_clobbered():
    # UPDATE matches on the old description, so a row edited meanwhile is skipped.
    db = _db()
    orig = db.execute

    def racing(sql, params=()):
        if sql.lstrip().startswith("UPDATE artifacts") and params[1] == "a1":
            db.orgs["o1"][0][2] = "user edit"
        return orig(sql, params)

    db.execute = racing
    stats = _run(db)
    assert db.orgs["o1"][0][2] == "user edit"
    assert stats["updated"] == 1


class TestRunBackfillOnce:
    def _go(self, redis, stats):
        with patch.object(pb, "_redis", return_value=redis), \
             patch.object(pb, "run_backfill", return_value=stats) as rb:
            return pb.run_backfill_once(), rb

    def test_marker_set_skips(self):
        r = MagicMock()
        r.get.return_value = b"1"
        out, rb = self._go(r, {"errors": 0})
        assert out == {"skipped": True}
        rb.assert_not_called()

    def test_clean_run_sets_marker(self):
        r = MagicMock()
        r.get.return_value = None
        out, rb = self._go(r, {"orgs": 1, "updated": 0, "errors": 0})
        rb.assert_called_once()
        r.set.assert_called_once_with(pb.DONE_KEY, "1")

    def test_errors_do_not_set_marker(self):
        r = MagicMock()
        r.get.return_value = None
        self._go(r, {"errors": 2})
        r.set.assert_not_called()

    def test_redis_raising_still_runs(self):
        r = MagicMock()
        r.get.side_effect = RuntimeError("down")
        r.set.side_effect = RuntimeError("down")
        out, rb = self._go(r, {"errors": 0})
        rb.assert_called_once()
        assert out == {"errors": 0}
