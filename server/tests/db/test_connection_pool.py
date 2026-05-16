"""Tests the Flask connection pool's tenant-isolation contract.

Pins SET-on-entry / RESET-on-exit behaviour so connections are never
returned to the pool with a prior tenant's RLS context attached.  Also
covers adversarial inputs to ``set_rls_context`` (invalid/hostile user IDs,
forgotten calls in Celery tasks) and post-fork pool recreation.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Provide POSTGRES_* defaults *before* importing connection_pool: a
# module-level singleton is created on import and reads these env vars.
os.environ.setdefault("POSTGRES_DB", "aurora_test")
os.environ.setdefault("POSTGRES_USER", "test_user")
os.environ.setdefault("POSTGRES_PASSWORD", "test_pw")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# The root conftest.py stubs flask/psycopg2 with MagicMock for tests that don't
# need them. This module needs real Flask (installed in CI) and a mock psycopg2
# pool, so we evict the stubs and re-import fresh.
for _mod in list(sys.modules):
    if _mod == "flask" or _mod.startswith("flask."):
        del sys.modules[_mod]
    if _mod == "dotenv":
        del sys.modules[_mod]
    if _mod.startswith("utils.db"):
        del sys.modules[_mod]

from flask import Flask, g  # noqa: E402

from utils.db import connection_pool as cp_module  # noqa: E402
from utils.db.connection_pool import DatabaseConnectionPool  # noqa: E402


_RESET_SQL = "RESET myapp.current_user_id; RESET myapp.current_org_id;"


def _make_conn():
    """Mock connection whose ``cursor()`` returns a context-manager mock."""
    cursor = MagicMock(name="cursor")
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    connection = MagicMock(name="connection")
    connection.cursor.return_value = cursor
    return connection, cursor


def _executed_sql(cursor):
    return [c.args[0] for c in cursor.execute.call_args_list if c.args]


@pytest.fixture()
def fresh_pool(monkeypatch):
    """Fresh ``DatabaseConnectionPool`` with psycopg2 mocked out."""
    monkeypatch.setenv("POSTGRES_DB", "aurora_test")
    monkeypatch.setenv("POSTGRES_USER", "test_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test_pw")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    monkeypatch.delenv("POSTGRES_SSLROOTCERT", raising=False)

    original_instance = DatabaseConnectionPool._instance
    DatabaseConnectionPool._instance = None
    pool_factory = MagicMock(name="ThreadedConnectionPool")
    monkeypatch.setattr("psycopg2.pool.ThreadedConnectionPool", pool_factory)

    try:
        yield DatabaseConnectionPool(), pool_factory
    finally:
        DatabaseConnectionPool._instance = original_instance


@pytest.fixture()
def flask_app():
    return Flask(__name__)


class TestSetRlsVarsFromRequest:
    """``_set_rls_vars`` must read identity from the Flask request and SET it."""

    def test_both_headers_set_both_vars(self, fresh_pool, flask_app):
        """X-User-ID + X-Org-ID -> both SET statements run before yield."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context(
            "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"},
        ):
            with pool.get_connection():
                executes = list(cursor.execute.call_args_list)

        sql = [c.args[0] for c in executes]
        params = [c.args[1] for c in executes if len(c.args) > 1]
        assert "SET myapp.current_user_id = %s" in sql
        assert "SET myapp.current_org_id = %s" in sql
        assert ("u-1",) in params
        assert ("org-7",) in params

    def test_only_user_id_header(self, fresh_pool, flask_app):
        """X-User-ID alone -> only current_user_id SET, no org SET."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context("/api/x", headers={"X-User-ID": "u-1"}):
            with pool.get_connection():
                sql = [c.args[0] for c in cursor.execute.call_args_list]

        assert "SET myapp.current_user_id = %s" in sql
        assert "SET myapp.current_org_id = %s" not in sql

    def test_org_id_falls_back_to_g_resolved(self, fresh_pool, flask_app):
        """When X-Org-ID is missing, ``g._org_id_resolved`` is used."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context("/api/x", headers={"X-User-ID": "u-1"}):
            g._org_id_resolved = "org-from-g"
            with pool.get_connection():
                params = [
                    c.args[1] for c in cursor.execute.call_args_list if len(c.args) > 1
                ]

        assert ("org-from-g",) in params

    def test_no_request_context_skips_set(self, fresh_pool):
        """No Flask request -> no SET, no raise."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with pool.get_connection():
            sql = [c.args[0] for c in cursor.execute.call_args_list]

        assert not any(s.startswith("SET myapp.") for s in sql)

    def test_request_context_without_identity_does_not_raise(self, fresh_pool, flask_app):
        """Request with no headers and no g._org_id_resolved must not raise."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context("/api/x"):
            with pool.get_connection():
                sql = [c.args[0] for c in cursor.execute.call_args_list]

        assert not any(s.startswith("SET myapp.") for s in sql)

    def test_set_failure_does_not_abort_yield(self, fresh_pool, flask_app):
        """If the SET cursor raises, get_connection still yields the conn."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        cursor.execute.side_effect = [Exception("set failed"), None, None]
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context(
            "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"},
        ):
            with pool.get_connection() as conn:
                assert conn is connection


class TestGetConnectionCleanup:
    """Connections must be RESET and returned, on every code path."""

    def test_reset_runs_on_normal_exit(self, fresh_pool, flask_app):
        """Both RESETs issued and putconn called after a clean yield."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with flask_app.test_request_context(
            "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"},
        ):
            with pool.get_connection() as conn:
                assert conn is connection

        assert _RESET_SQL in _executed_sql(cursor)
        connection.commit.assert_called()
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_reset_runs_when_yield_body_raised(self, fresh_pool, flask_app):
        """Exceptions inside the with-block do not skip the RESET."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        saw_runtime_error = False
        with flask_app.test_request_context(
            "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"},
        ):
            try:
                with pool.get_connection():
                    raise RuntimeError("caller-bug")
            except RuntimeError as exc:
                assert str(exc) == "caller-bug"
                saw_runtime_error = True

        assert saw_runtime_error
        assert _RESET_SQL in _executed_sql(cursor)
        connection.rollback.assert_called()
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_reset_runs_with_no_request_context(self, fresh_pool):
        """Even without a SET on entry, RESET still runs on exit."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        with pool.get_connection() as conn:
            assert conn is connection

        assert _RESET_SQL in _executed_sql(cursor)
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_failed_reset_does_not_block_putconn(self, fresh_pool, flask_app):
        """If the RESET execute raises, the connection is still returned."""
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection
        cursor.execute.side_effect = [None, None, Exception("conn lost")]

        with flask_app.test_request_context(
            "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"},
        ):
            with pool.get_connection() as conn:
                assert conn is connection

        assert _RESET_SQL in _executed_sql(cursor)
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_putconn_failure_swallowed(self, fresh_pool):
        """``pool.putconn`` raising must be logged, not propagated."""
        pool, factory = fresh_pool
        connection, _ = _make_conn()
        factory.return_value.getconn.return_value = connection
        factory.return_value.putconn.side_effect = Exception("pool down")

        with pool.get_connection() as conn:
            assert conn is connection

        factory.return_value.putconn.assert_called_once_with(connection)


class TestRLSContextAdversarial:
    """Adversarial inputs to set_rls_context and pool-release behaviour for
    tasks that forget to call it.

    Invariant: no malformed user_id, cross-org user_id, or absent RLS call
    must leave a previous tenant's identity attached to a returned connection.
    Every path must either refuse to configure RLS (returning None, emitting no
    SET) or guarantee that the pool's RESET fires on release.
    """

    # ------------------------------------------------------------------
    # Invalid / hostile user_id values passed to set_rls_context
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("bad_user_id", [
        None,
        "",
        "../etc/passwd",
        "org-b-victim-uuid",          # UUID belonging to a different org
        "\x00null-byte",
        "a" * 512,                    # pathologically long identifier
    ])
    def test_unresolvable_user_id_does_not_set_rls_vars(
        self, bad_user_id, monkeypatch
    ):
        """Any user_id for which org resolution returns None must result in
        zero SET executions and no commit — never a half-stamped connection.
        """
        from utils.auth import stateless_auth as sa_module
        from utils.auth.stateless_auth import set_rls_context

        # Simulate org lookup returning nothing for every hostile input.
        monkeypatch.setattr(sa_module, "_user_org_cache", {})
        monkeypatch.setattr(
            sa_module, "get_org_id_for_user", MagicMock(return_value=None)
        )

        cursor, conn = MagicMock(name="cursor"), MagicMock(name="conn")
        result = set_rls_context(cursor, conn, bad_user_id)

        assert result is None, (
            f"set_rls_context must return None for unresolvable user_id={bad_user_id!r}"
        )
        cursor.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_cross_org_user_id_resolved_to_foreign_org_does_not_set_requester_org(
        self, monkeypatch
    ):
        """A user_id that resolves to org-B must configure RLS for org-B —
        never silently leave org-A's vars from a prior borrow on the same
        connection.  The fixture confirms SET is called with the *resolved*
        org, not any caller-supplied one.
        """
        from utils.auth import stateless_auth as sa_module
        from utils.auth.stateless_auth import set_rls_context

        monkeypatch.setattr(sa_module, "_user_org_cache", {})
        monkeypatch.setattr(
            sa_module, "get_org_id_for_user", MagicMock(return_value="org-B")
        )

        cursor, conn = MagicMock(name="cursor"), MagicMock(name="conn")
        result = set_rls_context(cursor, conn, "user-in-org-B")

        assert result == "org-B"
        set_sql = [c.args[0] for c in cursor.execute.call_args_list]
        set_params = [c.args[1] for c in cursor.execute.call_args_list if len(c.args) > 1]
        assert "SET myapp.current_org_id = %s;" in set_sql
        assert ("org-B",) in set_params
        # Must NOT contain anything that looks like org-A
        assert ("org-A",) not in set_params

    # ------------------------------------------------------------------
    # Pool RESET fires even when set_rls_context was never called
    # (simulates a Celery task that forgets the call)
    # ------------------------------------------------------------------

    def test_reset_fires_when_celery_task_skips_set_rls_context(
        self, fresh_pool
    ):
        """A task that borrows a connection but never calls set_rls_context
        must still trigger the RESET on release — the connection must not
        be returned to the pool with stale session vars attached.
        """
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        factory.return_value.getconn.return_value = connection

        # No Flask request context → simulates a bare Celery worker invocation.
        with pool.get_connection() as conn:
            # Deliberately omit set_rls_context to mirror the Celery
            # "forgot to set RLS" scenario.
            assert conn is connection

        assert _RESET_SQL in _executed_sql(cursor), (
            "Pool RESET must run even when set_rls_context was never called."
        )
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_reset_fires_after_set_rls_context_raises(self, fresh_pool, flask_app):
        """If set_rls_context's underlying cursor.execute raises mid-way,
        the pool must still RESET and return the connection — a partial SET
        must not be left on a connection that goes back to the pool.
        """
        pool, factory = fresh_pool
        connection, cursor = _make_conn()
        # First execute (SET user_id) raises; subsequent calls (RESET) succeed.
        cursor.execute.side_effect = [Exception("db gone"), None, None]
        factory.return_value.getconn.return_value = connection

        with (
            flask_app.test_request_context(
                "/api/x", headers={"X-User-ID": "u-1", "X-Org-ID": "org-7"}
            ),
            pool.get_connection() as conn,
        ):
            assert conn is connection

        assert _RESET_SQL in _executed_sql(cursor)
        factory.return_value.putconn.assert_called_once_with(connection)



class TestPostForkPoolRecreation:
    """Forked workers must drop the inherited pool and create a new one."""

    def test_pool_recreated_when_pid_changes(self, fresh_pool, monkeypatch):
        """Different ``os.getpid()`` -> ThreadedConnectionPool called twice."""
        pool, factory = fresh_pool
        parent, child = MagicMock(name="parent_pool"), MagicMock(name="child_pool")
        factory.side_effect = [parent, child]

        monkeypatch.setattr(cp_module.os, "getpid", lambda: 100)
        assert pool._get_pool() is parent
        assert pool._pool_pid == 100

        monkeypatch.setattr(cp_module.os, "getpid", lambda: 200)
        assert pool._get_pool() is child
        assert pool._pool_pid == 200
        assert factory.call_count == 2

    def test_pool_reused_when_pid_unchanged(self, fresh_pool, monkeypatch):
        """Same PID across calls -> ThreadedConnectionPool called only once."""
        pool, factory = fresh_pool
        monkeypatch.setattr(cp_module.os, "getpid", lambda: 100)

        assert pool._get_pool() is pool._get_pool()
        assert factory.call_count == 1
