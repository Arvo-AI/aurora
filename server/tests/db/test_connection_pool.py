"""Tests for utils.db.connection_pool -- RLS session variable handling.

Locks down the ``DatabaseConnectionPool.get_connection`` /
``_set_rls_vars`` contract so a connection is never returned to the pool
with another tenant's RLS context still attached.
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
            with pool.get_connection():
                pass

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

        with pool.get_connection():
            pass

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
            with pool.get_connection():
                pass

        assert _RESET_SQL in _executed_sql(cursor)
        factory.return_value.putconn.assert_called_once_with(connection)

    def test_putconn_failure_swallowed(self, fresh_pool):
        """``pool.putconn`` raising must be logged, not propagated."""
        pool, factory = fresh_pool
        connection, _ = _make_conn()
        factory.return_value.getconn.return_value = connection
        factory.return_value.putconn.side_effect = Exception("pool down")

        with pool.get_connection():
            pass

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
