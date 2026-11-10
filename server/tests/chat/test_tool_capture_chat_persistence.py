"""Regular-chat tool calls are recorded in execution_steps (DEV-1729).

Before, only incident (RCA) sessions persisted steps, so a failed tool call in
an ordinary chat could only be found by opening that chat.
"""

from unittest.mock import MagicMock

import pytest

import chat.backend.agent.utils.tool_context_capture as mod
from chat.backend.agent.utils.tool_context_capture import ToolContextCapture


@pytest.fixture
def db(monkeypatch):
    cur = MagicMock()
    cur.fetchone.return_value = (42,)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(mod, "db_pool", pool)
    monkeypatch.setattr(mod, "set_rls_context", lambda *a, **k: "org-1")
    return cur


def test_chat_session_persists_and_counts_steps_per_session(db):
    cap = ToolContextCapture("sess-1", "user-1", incident_id=None, org_id="org-1")
    assert cap._persist_steps

    assert cap._record_step_start("cloud_exec", {"command": "s3 ls"}, "tc-1") == 42
    sql, params = db.execute.call_args.args
    assert "incident_id IS NULL AND session_id = %s" in sql
    assert params[0] is None            # incident_id
    assert params[2] == "org-1"         # org_id set explicitly (no parent incident)
    assert params[-1] == "sess-1"


def test_incident_session_still_counts_steps_per_incident(db):
    cap = ToolContextCapture("sess-1", "user-1", incident_id="inc-1", org_id="org-1")
    cap._record_step_start("cloud_exec", {}, "tc-1")
    sql, params = db.execute.call_args.args
    assert "WHERE incident_id = %s" in sql
    assert params[-1] == "inc-1"


def test_chat_org_is_resolved_when_not_given(monkeypatch):
    import utils.auth.stateless_auth as sa
    monkeypatch.setattr(sa, "get_org_id_for_user", lambda uid: "org-9")
    cap = ToolContextCapture("sess-1", "user-1")
    assert cap.org_id == "org-9" and cap._persist_steps


def test_no_org_disables_persistence(monkeypatch):
    import utils.auth.stateless_auth as sa
    monkeypatch.setattr(sa, "get_org_id_for_user", lambda uid: None)
    cap = ToolContextCapture("sess-1", "user-1")
    assert not cap._persist_steps
    assert cap._record_step_start("x", {}) is None


def test_org_moves_carry_chat_tool_calls():
    from utils.db import org_backfill

    for fn in (org_backfill.backfill_user_org_data, org_backfill.migrate_user_to_org):
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.rowcount = 3
        fn(cur, "user-1", "org-2")
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        moved = [s for s in sqls if "UPDATE execution_steps es" in s]
        assert moved, fn.__name__
        assert "es.incident_id IS NULL" in moved[0]
        assert "split_part(es.session_id, '::', 1)" in moved[0]
