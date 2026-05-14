"""Tests for the iter-2 hardening pass.

Each test pins one of the three additional fixes: connection leak on
lock-acquisition failure, RLS context leak in the linker per-org
loop, and the per-event try-lock that dedups parallel
launch_investigation runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from services.change_intercept import tasks as tasks_module
from services.change_intercept.tasks import (
    _event_lock_token,
    _try_advisory_lock,
)


# ─── Per-event try-lock dedup ────────────────────────────────────────


def test_event_lock_token_is_stable_for_same_id() -> None:
    a = _event_lock_token("abc-123")
    b = _event_lock_token("abc-123")
    assert a == b
    assert 0 < a < 2**63


def test_event_lock_token_differs_across_ids() -> None:
    assert _event_lock_token("abc-123") != _event_lock_token("abc-124")


def test_try_advisory_lock_returns_true_on_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful pg_try_advisory_lock yields True; the caller proceeds
    with the investigation."""
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = (True,)
    fake_cursor_cm = MagicMock()
    fake_cursor_cm.__enter__ = lambda _self: fake_cursor
    fake_cursor_cm.__exit__ = lambda *_a: None

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor_cm

    fake_conn_cm = MagicMock()
    fake_conn_cm.__enter__ = lambda _self: fake_conn
    fake_conn_cm.__exit__ = lambda *_a: None

    fake_pool = MagicMock()
    fake_pool.get_admin_connection.return_value = fake_conn_cm

    import utils.db.connection_pool as cp_mod

    monkeypatch.setattr(cp_mod, "db_pool", fake_pool)

    with _try_advisory_lock(42) as got:
        assert got is True


def test_try_advisory_lock_returns_false_on_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pg_try_advisory_lock returns FALSE, the caller observes
    False and skips the work — no retry, no duplicate investigation."""
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = (False,)
    fake_cursor_cm = MagicMock()
    fake_cursor_cm.__enter__ = lambda _self: fake_cursor
    fake_cursor_cm.__exit__ = lambda *_a: None

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor_cm

    fake_conn_cm = MagicMock()
    fake_conn_cm.__enter__ = lambda _self: fake_conn
    fake_conn_cm.__exit__ = lambda *_a: None

    fake_pool = MagicMock()
    fake_pool.get_admin_connection.return_value = fake_conn_cm

    import utils.db.connection_pool as cp_mod

    monkeypatch.setattr(cp_mod, "db_pool", fake_pool)

    with _try_advisory_lock(42) as got:
        assert got is False


def test_try_advisory_lock_releases_connection_when_acquire_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LOCK SQL itself raises (DB blip), the pool checkout MUST
    still be returned. Otherwise we leak a connection per failure."""
    fake_cursor = MagicMock()
    fake_cursor.execute.side_effect = RuntimeError("simulated DB blip")
    fake_cursor_cm = MagicMock()
    fake_cursor_cm.__enter__ = lambda _self: fake_cursor
    fake_cursor_cm.__exit__ = lambda *_a: None

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor_cm

    exit_calls: list[tuple] = []

    class FakeCM:
        def __enter__(self):
            return fake_conn

        def __exit__(self, *args):
            exit_calls.append(args)
            return False

    fake_pool = MagicMock()
    fake_pool.get_admin_connection.return_value = FakeCM()

    import utils.db.connection_pool as cp_mod

    monkeypatch.setattr(cp_mod, "db_pool", fake_pool)

    with pytest.raises(RuntimeError):
        with _try_advisory_lock(42):
            pass

    # The cleanup path MUST have invoked __exit__ on the conn cm.
    assert exit_calls, "connection context manager was not closed on lock-acquire failure"
