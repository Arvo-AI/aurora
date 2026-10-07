"""PROTECTED_ENTRIES: well-known memory entries keep a stable (category, title).

The Slack policy lives in a user-writable category on purpose (both the user and
the agent edit its content), but the agent's prompt injector and the seeder pin to
its (category, title) pair. Renaming/recategorizing/deleting it would silently
detach it — no error, Aurora just stops applying the policy — so both the memory
routes and the agent's own memory tools reject those operations.

These tests drive the real guards: HTTP requests through the routes, and direct
calls into the agent tools. The identity constants are asserted only where a
silent mismatch is the actual failure mode (the injector's key format).
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from services.memory import (
    PROTECTED_ENTRIES,
    SLACK_MEMORY_CATEGORY,
    SLACK_MEMORY_TITLE,
    SYSTEM_CATEGORY,
    USER_WRITABLE_CATEGORIES,
)

ORG_ID = "org-test"
USER_ID = "user-test"
ENTRY_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Identity contract — only what fails silently if it drifts
# ---------------------------------------------------------------------------

def test_slack_memory_stays_user_writable():
    """Protection must be an identity lock, not a read-only flag.

    Implementing it by moving the entry into SYSTEM_CATEGORY would make the
    content read-only, which is the opposite of what's wanted: users and the
    agent are both meant to edit the policy freely.
    """
    assert (SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE) in PROTECTED_ENTRIES
    assert SLACK_MEMORY_CATEGORY in USER_WRITABLE_CATEGORIES
    assert SLACK_MEMORY_CATEGORY != SYSTEM_CATEGORY


def test_agent_injector_key_matches_the_protected_identity():
    """The injector's force_entries key must match the protected pair.

    Guards against the constants drifting from the literal the injector builds:
    a mismatch here means Slack sessions silently stop getting the policy, which
    is exactly the failure this whole registry exists to prevent.
    """
    from services.memory.injector import _entry_key

    entry = {"category": SLACK_MEMORY_CATEGORY, "title": SLACK_MEMORY_TITLE}
    assert _entry_key(entry) == f"{SLACK_MEMORY_CATEGORY}/{SLACK_MEMORY_TITLE}"


# ---------------------------------------------------------------------------
# HTTP routes — PUT rename/recategorize and DELETE must 403
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_client(monkeypatch):
    """Flask test client for memory_bp with the DB and auth stubbed out.

    The cursor returns the protected Slack entry for the guards' identity
    lookup, so each test exercises the real branch in the route.
    """
    pytest.importorskip("flask")

    # Evict so Werkzeug proxies bind to the app this fixture creates.
    for mod in [m for m in list(sys.modules) if m.startswith(("routes.", "utils.auth.rbac"))]:
        del sys.modules[mod]
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        sys.modules.setdefault(heavy, MagicMock())
    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask

    from routes.memory import routes as memory_routes
    from utils.auth import rbac_decorators as rbac

    # Authorize every request so only the protected-entry guard is under test.
    monkeypatch.setattr(rbac, "get_user_id_from_request", lambda *a, **k: USER_ID)
    monkeypatch.setattr(rbac, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(rbac, "enforce_with_reload", lambda *a, **k: True)
    monkeypatch.setattr(rbac, "_audit_auth_failure", lambda *a, **k: None)

    monkeypatch.setattr(memory_routes, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(memory_routes, "set_rls_context", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(memory_routes, "get_user_display_name", lambda *a, **k: "Tester")

    cursor = MagicMock()
    # Both guards SELECT (category, title) to identify the target entry.
    cursor.fetchone.return_value = (SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE)
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.get_user_connection.return_value.__enter__ = lambda s: conn
    pool.get_user_connection.return_value.__exit__ = lambda s, *a: False
    monkeypatch.setattr(memory_routes, "db_pool", pool)

    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(memory_routes.memory_bp)
    client = app.test_client()
    client.cursor = cursor
    return client


def _put(client, body):
    """PUT the protected entry with an authenticated identity."""
    return client.put(
        f"/entries/{ENTRY_ID}",
        json=body,
        headers={"X-User-ID": USER_ID, "X-Org-ID": ORG_ID},
    )


def test_route_rename_of_protected_entry_is_rejected(memory_client):
    """A new title would detach the entry from the injector's (category, title) key."""
    resp = _put(memory_client, {"title": "Slack Policy"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "protected_entry"


def test_route_recategorize_of_protected_entry_is_rejected(memory_client):
    """Moving categories breaks the same lookup as a rename."""
    resp = _put(memory_client, {"category": "runbook"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "protected_entry"


def test_route_delete_of_protected_entry_is_rejected(memory_client):
    """Deleting it would leave Slack with no policy at all."""
    resp = memory_client.delete(
        f"/entries/{ENTRY_ID}",
        headers={"X-User-ID": USER_ID, "X-Org-ID": ORG_ID},
    )
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "protected_entry"
    # The guard must return before any DELETE reaches the DB.
    assert not any(
        "DELETE" in str(c.args[0]).upper()
        for c in memory_client.cursor.execute.call_args_list
    )


def test_route_content_edit_of_protected_entry_is_allowed(memory_client):
    """The whole point of an identity lock: content stays freely editable."""
    with patch("routes.memory.routes.create_version", return_value=2):
        resp = _put(memory_client, {"content": "updated policy body"})
    assert resp.status_code == 200


def test_route_resending_the_unchanged_title_is_not_a_rename(memory_client):
    """The UI round-trips the full entry, so an unchanged title must not 403."""
    with patch("routes.memory.routes.create_version", return_value=2):
        resp = _put(
            memory_client,
            {"title": SLACK_MEMORY_TITLE, "category": SLACK_MEMORY_CATEGORY,
             "content": "updated policy body"},
        )
    assert resp.status_code == 200


def test_route_rename_of_unprotected_entry_is_allowed(memory_client):
    """Only registered entries are locked; ordinary memory is untouched."""
    memory_client.cursor.fetchone.return_value = ("runbook", "Deploy Steps")
    resp = _put(memory_client, {"title": "Deployment Steps"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Agent tools — the same identity must be protected from the agent itself
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_tool(monkeypatch):
    """The agent's memory_tool with its DB connection stubbed."""
    from chat.backend.agent.tools import memory_tool as mod

    cursor = MagicMock()
    cursor.fetchone.return_value = (ENTRY_ID,)
    conn = MagicMock()

    class _Conn:
        """Stands in for _memory_connection's (cursor, conn, org_id) contract."""

        def __enter__(self):
            return cursor, conn, ORG_ID

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_memory_connection", lambda *a, **k: _Conn())
    mod.cursor = cursor
    return mod


def test_agent_cannot_rename_protected_entry(memory_tool):
    """The agent is the other writer, so it needs the same lock as the routes."""
    result = json.loads(memory_tool.rename_memory(
        category=SLACK_MEMORY_CATEGORY,
        title=SLACK_MEMORY_TITLE,
        new_title="Slack Policy",
        user_id=USER_ID,
    ))
    assert result["code"] == "protected_entry"
    memory_tool.cursor.execute.assert_not_called()


def test_agent_cannot_delete_protected_entry(memory_tool):
    """Blocked before the connection opens, so nothing reaches the DB."""
    result = json.loads(memory_tool.delete_memory(
        category=SLACK_MEMORY_CATEGORY,
        title=SLACK_MEMORY_TITLE,
        user_id=USER_ID,
    ))
    assert result["code"] == "protected_entry"
    memory_tool.cursor.execute.assert_not_called()


def test_agent_can_update_protected_entry_description(memory_tool):
    """Description-only edits leave the identity intact, so they're allowed."""
    result = json.loads(memory_tool.rename_memory(
        category=SLACK_MEMORY_CATEGORY,
        title=SLACK_MEMORY_TITLE,
        new_description="How Aurora behaves in Slack",
        user_id=USER_ID,
    ))
    assert result["status"] == "ok"


def test_agent_can_rename_unprotected_entry(memory_tool):
    """Ordinary memory entries stay fully renameable by the agent."""
    result = json.loads(memory_tool.rename_memory(
        category="runbook",
        title="Deploy Steps",
        new_title="Deployment Steps",
        user_id=USER_ID,
    ))
    assert result["status"] == "ok"
