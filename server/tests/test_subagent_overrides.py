"""Tests for catalog override filtering and fail-open behaviour."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages.
for _pkg in (
    "langchain_core",
    "langchain_core.messages",
):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

from chat.backend.agent.orchestrator import catalog as catalog_mod  # noqa: E402
from chat.backend.agent.orchestrator.catalog import (  # noqa: E402
    BUILTIN_CATALOG,
    get_enabled_catalog,
)


def _fake_db_pool(rows):
    """Build a context-manager-friendly fake db_pool whose cursor.fetchall returns rows."""
    fake_cursor = MagicMock()
    fake_cursor.fetchall.return_value = rows
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)

    fake_pool = MagicMock()
    fake_pool.get_admin_connection.return_value = fake_conn
    return fake_pool, fake_cursor


# ---------------------------------------------------------------------------
# get_enabled_catalog: include all when no overrides
# ---------------------------------------------------------------------------


def test_catalog_includes_all_when_no_overrides():
    """No override rows → full BUILTIN_CATALOG returned."""
    fake_pool, _ = _fake_db_pool(rows=[])
    with patch("utils.db.connection_pool.db_pool", fake_pool):
        result = get_enabled_catalog("org-1")
    assert set(result.keys()) == set(BUILTIN_CATALOG.keys())


# ---------------------------------------------------------------------------
# get_enabled_catalog: exclude disabled
# ---------------------------------------------------------------------------


def test_catalog_excludes_disabled():
    """A disabled subagent_id row removes that entry from the catalog."""
    fake_pool, _ = _fake_db_pool(rows=[("builtin:db",)])
    with patch("utils.db.connection_pool.db_pool", fake_pool):
        result = get_enabled_catalog("org-1")
    assert "builtin:db" not in result
    expected = set(BUILTIN_CATALOG.keys()) - {"builtin:db"}
    assert set(result.keys()) == expected


# ---------------------------------------------------------------------------
# get_enabled_catalog: fail open on DB error
# ---------------------------------------------------------------------------


def test_catalog_fail_open_on_db_error():
    """If db_pool.get_admin_connection raises, fall back to full catalog."""
    fake_pool = MagicMock()
    fake_pool.get_admin_connection.side_effect = RuntimeError("db unreachable")
    with patch("utils.db.connection_pool.db_pool", fake_pool):
        result = get_enabled_catalog("org-1")
    # given DB is down, when we ask for the catalog, then fail-open returns
    # the full BUILTIN_CATALOG so RCAs aren't blocked
    assert set(result.keys()) == set(BUILTIN_CATALOG.keys())


def test_catalog_empty_org_id_returns_full_catalog():
    """Empty org_id → return full catalog without touching the DB."""
    fake_pool = MagicMock()
    fake_pool.get_admin_connection.side_effect = AssertionError(
        "DB must not be touched for empty org_id"
    )
    with patch("utils.db.connection_pool.db_pool", fake_pool):
        result = get_enabled_catalog("")
    assert set(result.keys()) == set(BUILTIN_CATALOG.keys())


# ---------------------------------------------------------------------------
# RLS isolation: SET myapp.current_org_id is called with the right org
# ---------------------------------------------------------------------------


def test_overrides_rls_isolation():
    """The cursor must be told myapp.current_org_id = <our org_id>."""
    fake_pool, fake_cursor = _fake_db_pool(rows=[])

    with patch("utils.db.connection_pool.db_pool", fake_pool):
        get_enabled_catalog("org-XYZ")

    # given the cursor's execute calls, find the SET statement and assert
    # it was bound to the org we asked about
    set_calls = [
        c for c in fake_cursor.execute.call_args_list
        if "myapp.current_org_id" in (c.args[0] if c.args else "")
    ]
    assert set_calls, "expected a SET myapp.current_org_id statement"
    # the second positional arg is the params tuple
    params = set_calls[0].args[1]
    assert params == ("org-XYZ",)
