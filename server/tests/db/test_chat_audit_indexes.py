"""Chat audit indexes are built concurrently, once, and never block or fail startup."""

from unittest.mock import MagicMock

from utils.db import chat_audit_indexes as idx


class FakeCursor:
    def __init__(self, *, lock=True, existing=None):
        self.lock, self.existing, self.sql = lock, existing or {}, []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.sql.append(sql)
        if "pg_try_advisory_lock" in sql:
            self._row = (self.lock,)
        elif "indisvalid" in sql:
            valid = self.existing.get(params[0])
            self._row = None if valid is None else (valid,)

    def fetchone(self):
        return self._row


def _run(cur):
    conn = MagicMock()
    conn.cursor.return_value = cur
    idx.build_indexes(lambda: conn)
    return conn


def test_builds_every_index_concurrently_outside_a_transaction():
    cur = FakeCursor()
    conn = _run(cur)
    assert conn.autocommit is True
    creates = [s for s in cur.sql if s.startswith("CREATE INDEX")]
    assert len(creates) == len(idx.INDEXES)
    assert all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in creates)
    assert "pg_advisory_unlock" in cur.sql[-1]
    conn.close.assert_called_once()


def test_retired_indexes_are_dropped_concurrently():
    cur = FakeCursor()
    _run(cur)
    for name in idx.RETIRED_INDEXES:
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {name}" in cur.sql


def test_search_expression_strips_json_escapes_before_tokenizing():
    # A line break is the two characters \n in the JSON text: without this the
    # next word would be indexed as "nthe".
    assert "regexp_replace(" in idx.SEARCH_DOC_SQL and r"'\\[nrt\\]', ' ', 'g'" in idx.SEARCH_DOC_SQL


def test_another_process_holding_the_lock_skips_the_build():
    cur = FakeCursor(lock=False)
    _run(cur)
    assert not any(s.startswith(("CREATE", "DROP")) for s in cur.sql)


def test_valid_indexes_are_left_alone_and_invalid_ones_rebuilt():
    name_ok, name_bad = idx.INDEXES[0][0], idx.INDEXES[1][0]
    cur = FakeCursor(existing={name_ok: True, name_bad: False})
    _run(cur)
    assert not any(name_ok in s for s in cur.sql if s.startswith("CREATE"))
    drop = next(i for i, s in enumerate(cur.sql) if s.startswith("DROP INDEX CONCURRENTLY") and name_bad in s)
    create = next(i for i, s in enumerate(cur.sql) if s.startswith("CREATE") and name_bad in s)
    assert drop < create


def test_errors_never_reach_startup():
    def boom():
        raise RuntimeError("db down")
    idx.build_indexes(boom)  # no exception
