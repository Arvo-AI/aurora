"""Startup migration lock waits a bounded time instead of forever."""

from unittest.mock import MagicMock

import pytest

from utils.db import migration_lock as ml


def _conn(try_results):
    """Connection whose pg_try_advisory_lock returns the given values in order."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.side_effect = [(r,) for r in try_results]
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _lock_calls(cur):
    return [c for c in cur.execute.call_args_list if "pg_try_advisory_lock" in c.args[0]]


def test_acquires_immediately_when_free(monkeypatch):
    monkeypatch.setattr(ml.time, "sleep", lambda _s: None)
    conn, cur = _conn([True])

    ml.acquire_migration_lock(conn, wait_seconds=5)

    assert len(_lock_calls(cur)) == 1


def test_polls_until_holder_releases(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ml.time, "sleep", sleeps.append)
    conn, cur = _conn([False, False, True])

    ml.acquire_migration_lock(conn, wait_seconds=60)

    assert len(_lock_calls(cur)) == 3
    assert len(sleeps) == 2


def test_raises_when_holder_never_releases(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(ml.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(ml.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    conn, cur = _conn([False] * 50)

    with pytest.raises(ml.MigrationLockTimeout):
        ml.acquire_migration_lock(conn, wait_seconds=5)

    # Bounded: roughly one poll per second for the wait window, not unbounded
    assert len(_lock_calls(cur)) <= 7


def test_enables_server_side_keepalives_before_locking(monkeypatch):
    monkeypatch.setattr(ml.time, "sleep", lambda _s: None)
    conn, cur = _conn([True])

    ml.acquire_migration_lock(conn, wait_seconds=5)

    statements = [c.args[0] for c in cur.execute.call_args_list]
    assert statements[0].startswith("SET tcp_keepalives")
    assert "pg_try_advisory_lock" in statements[-1]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 240.0),
        ("90", 90.0),
        ("garbage", 240.0),
        ("nan", 240.0),
        ("inf", 240.0),
        ("0", 240.0),
        ("-5", 240.0),
    ],
)
def test_wait_seconds_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("DB_MIGRATION_LOCK_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("DB_MIGRATION_LOCK_TIMEOUT", raw)
    assert ml._wait_seconds() == expected
