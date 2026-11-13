"""Regular-chat tool calls are recorded in execution_steps.

Before, only incident (RCA) sessions persisted steps, so a failed tool call in
an ordinary chat could only be found by opening that chat.
"""

from unittest.mock import MagicMock

import pytest

import chat.backend.agent.utils.tool_context_capture as mod

ToolContextCapture = mod.ToolContextCapture


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

    step = cap._record_step_start("cloud_exec", {"command": "s3 ls"}, "tc-1")
    assert step.result(timeout=5) == 42   # regular chats write off the event loop
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


def test_incident_steps_stay_synchronous(db):
    cap = ToolContextCapture("sess-1", "user-1", incident_id="inc-1", org_id="org-1")
    assert cap._record_step_start("cloud_exec", {}, "tc-1") == 42


def test_chat_step_end_runs_after_its_start_with_call_time_timestamp(db, monkeypatch):
    import threading
    from concurrent.futures import Future

    gate = threading.Event()
    real_insert = ToolContextCapture._insert_step

    def slow_insert(self, *args):
        gate.wait(timeout=5)                       # hold the INSERT until end() is queued
        return real_insert(self, *args)

    monkeypatch.setattr(ToolContextCapture, "_insert_step", slow_insert)
    cap = ToolContextCapture("sess-1", "user-1", org_id="org-1")
    step = cap._record_step_start("cloud_exec", {}, "tc-1")
    assert isinstance(step, Future)
    cap._record_step_end(step, '{"error": "AccessDenied"}')
    gate.set()
    mod._STEP_WRITER.submit(lambda: None).result(timeout=5)   # drain the queue
    for _ in range(50):
        if any("UPDATE execution_steps" in c.args[0] for c in db.execute.call_args_list):
            break
        threading.Event().wait(0.02)
    statements = [c.args[0] for c in db.execute.call_args_list if c.args[0].lstrip().startswith(("INSERT", "UPDATE"))]
    assert "INSERT" in statements[0] and "UPDATE" in statements[-1]
    update_params = next(c.args[1] for c in db.execute.call_args_list if "UPDATE execution_steps" in c.args[0])
    assert update_params[0] == "error" and update_params[-1] == 42


def test_large_tool_inputs_are_capped_and_stay_valid_json():
    import json
    stored = mod._capped_input_json({"command": "x" * 50_000})
    assert len(stored) < mod.STEP_INPUT_MAX_CHARS + 100
    assert json.loads(stored)["_truncated"] is True
    assert json.loads(mod._capped_input_json({"command": "ls"})) == {"command": "ls"}


def test_step_index_allocation_is_serialized_per_chat(db):
    cap = ToolContextCapture("sess-1", "user-1", incident_id="inc-1", org_id="org-1")
    cap._record_step_start("cloud_exec", {}, "tc-1")
    sqls = [c.args[0] for c in db.execute.call_args_list]
    lock = next(i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql)
    insert = next(i for i, sql in enumerate(sqls) if "INSERT INTO execution_steps" in sql)
    assert lock < insert                                         # same transaction, before MAX+1
    assert db.execute.call_args_list[lock].args[1] == ("execution_steps:inc-1",)
    chat = ToolContextCapture("sess-9", "user-1", org_id="org-1")
    chat._record_step_start("cloud_exec", {}, "tc-2").result(timeout=5)
    assert ("execution_steps:sess-9",) in [c.args[1] for c in db.execute.call_args_list
                                           if "pg_advisory_xact_lock" in c.args[0]]
