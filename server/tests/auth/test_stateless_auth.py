"""Tests the Postgres Row-Level Security bootstrap that Celery workers,
LangGraph tasks, and other non-Flask callers must run before querying
RLS-protected tables. Pins the happy path (set user_id, set org_id,
commit) and the fail-closed path (org unresolved -> no SET, no commit,
return None) so a half-stamped connection can't leak rows across
tenants.
"""

from unittest.mock import MagicMock

import pytest

from utils.auth import stateless_auth as stateless_auth_module
from utils.auth.stateless_auth import set_rls_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cursor_and_conn():
    return MagicMock(name="cursor"), MagicMock(name="connection")


def _executed(cursor):
    """Yield ``(sql, params)`` tuples for every cursor.execute call."""
    for call in cursor.execute.call_args_list:
        sql = call.args[0] if call.args else None
        params = call.args[1] if len(call.args) > 1 else None
        yield sql, params


@pytest.fixture(autouse=True)
def _clear_org_cache(monkeypatch):
    """Wipe the 5-minute in-process cache so test ordering can't leak state."""
    monkeypatch.setattr(stateless_auth_module, "_user_org_cache", {})


@pytest.fixture
def patch_org_lookup(monkeypatch):
    """Stub ``get_org_id_for_user`` with a configurable return value."""
    def _patch(return_value):
        monkeypatch.setattr(
            stateless_auth_module,
            "get_org_id_for_user",
            MagicMock(return_value=return_value),
        )
    return _patch


# ---------------------------------------------------------------------------
# Happy path: org resolves -> both SETs run, commit, return org_id
# ---------------------------------------------------------------------------


class TestSetRlsContextHappyPath:
    """When the org lookup returns a value, both RLS variables get set."""

    def test_returns_resolved_org_id(self, patch_org_lookup):
        patch_org_lookup("org-7")
        cursor, conn = _make_cursor_and_conn()

        assert set_rls_context(cursor, conn, "u-1") == "org-7"

    def test_sets_user_id_then_org_id(self, patch_org_lookup):
        """``current_user_id`` first, ``current_org_id`` second."""
        patch_org_lookup("org-7")
        cursor, conn = _make_cursor_and_conn()

        set_rls_context(cursor, conn, "u-1")

        executed = list(_executed(cursor))
        assert executed == [
            ("SET myapp.current_user_id = %s;", ("u-1",)),
            ("SET myapp.current_org_id = %s;", ("org-7",)),
        ]

    def test_commits_after_setting_vars(self, patch_org_lookup):
        patch_org_lookup("org-7")
        cursor, conn = _make_cursor_and_conn()

        set_rls_context(cursor, conn, "u-1")

        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Fail-closed: org cannot be resolved -> no SET, no commit, return None
# ---------------------------------------------------------------------------


class TestSetRlsContextOrgUnresolvable:
    """No org -> abort. Half-configuring the connection silently breaks RLS."""

    def test_none_org_returns_none(self, patch_org_lookup):
        patch_org_lookup(None)
        cursor, conn = _make_cursor_and_conn()

        assert set_rls_context(cursor, conn, "u-1") is None

    def test_none_org_does_not_execute_any_set(self, patch_org_lookup):
        patch_org_lookup(None)
        cursor, conn = _make_cursor_and_conn()

        set_rls_context(cursor, conn, "u-1")

        cursor.execute.assert_not_called()

    def test_none_org_does_not_commit(self, patch_org_lookup):
        patch_org_lookup(None)
        cursor, conn = _make_cursor_and_conn()

        set_rls_context(cursor, conn, "u-1")

        conn.commit.assert_not_called()

    def test_empty_string_org_treated_as_unresolved(self, patch_org_lookup):
        """Falsy org -> same fail-closed path as None."""
        patch_org_lookup("")
        cursor, conn = _make_cursor_and_conn()

        result = set_rls_context(cursor, conn, "u-1")

        assert result is None
        cursor.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_log_prefix_included_in_error(self, patch_org_lookup, caplog):
        """``log_prefix`` lets operators identify which task failed."""
        patch_org_lookup(None)
        cursor, conn = _make_cursor_and_conn()

        with caplog.at_level("ERROR", logger=stateless_auth_module.logger.name):
            set_rls_context(cursor, conn, "u-1", log_prefix="[MyTask]")

        assert any("[MyTask]" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Lookup wiring
# ---------------------------------------------------------------------------


class TestOrgLookupWiring:
    """``set_rls_context`` delegates org resolution; it does not reimplement it."""

    def test_calls_get_org_id_for_user_with_user_id(self, monkeypatch):
        org_lookup = MagicMock(return_value="org-7")
        monkeypatch.setattr(
            stateless_auth_module, "get_org_id_for_user", org_lookup,
        )
        cursor, conn = _make_cursor_and_conn()

        set_rls_context(cursor, conn, "u-1")

        org_lookup.assert_called_once_with("u-1")


# ---------------------------------------------------------------------------
# Hostile user_id inputs
# ---------------------------------------------------------------------------


class TestSetRlsContextHostileInputs:
    """Malformed or adversarial user_id values must never produce a
    half-stamped connection — if org resolution returns None the function
    must emit no SET and no commit, regardless of what the user_id looks like.
    """

    @pytest.mark.parametrize("bad_user_id", [
        None,
        "",
        "../etc/passwd",
        "org-b-victim-uuid",
        "\x00null-byte",
        "a" * 512,
    ])
    def test_unresolvable_user_id_does_not_set_rls_vars(
        self, bad_user_id, patch_org_lookup
    ):
        """Any user_id for which org resolution returns None → no SET, no commit."""
        patch_org_lookup(None)
        cursor, conn = _make_cursor_and_conn()

        result = set_rls_context(cursor, conn, bad_user_id)

        assert result is None
        cursor.execute.assert_not_called()
        conn.commit.assert_not_called()

    def test_foreign_org_user_id_sets_resolved_org_not_caller_org(
        self, patch_org_lookup
    ):
        """A user_id resolving to org-B must stamp the connection with org-B —
        never leave a prior tenant's org_id from an earlier pool borrow.
        """
        patch_org_lookup("org-B")
        cursor, conn = _make_cursor_and_conn()

        result = set_rls_context(cursor, conn, "user-in-org-B")

        assert result == "org-B"
        executed = list(_executed(cursor))
        sql_stmts = [sql for sql, _ in executed]
        params = [p for _, p in executed if p is not None]
        assert "SET myapp.current_org_id = %s;" in sql_stmts
        assert ("org-B",) in params
        assert ("org-A",) not in params
