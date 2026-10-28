"""Tests for issue #566: Next Steps suggestion status reflecting actual command result.

Verifies:
1. _parse_command_output accurately parses exit codes, success, stdout, and stderr.
2. _record_suggestion_command_result persists structured command results and bumps incident updated_at.
3. _propagate_suggestion_status NEVER overwrites an already-recorded command result with session status.
4. _propagate_suggestion_status recovers tool results from chat_sessions messages if execution_result was NULL.
5. mark_suggestion_executed endpoint accepts executionStatus, touches incidents.updated_at, and links chat session.
"""

import json
import uuid
from unittest.mock import MagicMock, call

import pytest
from flask import Flask

from chat.background.task import (
    _parse_command_output,
    _record_suggestion_command_result,
    _propagate_suggestion_status,
)
import routes.incidents_routes as incidents_routes


def test_parse_command_output_success_json():
    payload = json.dumps({
        "success": True,
        "command": "kubectl rollout restart deployment/api",
        "return_code": 0,
        "chat_output": "deployment.apps/api restarted",
    })
    res = _parse_command_output(payload)
    assert res["success"] is True
    assert res["exit_code"] == 0
    assert res["output"] == "deployment.apps/api restarted"
    assert res["error"] is None


def test_parse_command_output_failure_json():
    payload = json.dumps({
        "success": False,
        "command": "kubectl rollout restart deployment/nonexistent",
        "return_code": 1,
        "error": "Error from server (NotFound): deployments.apps \"nonexistent\" not found",
    })
    res = _parse_command_output(payload)
    assert res["success"] is False
    assert res["exit_code"] == 1
    assert "NotFound" in res["error"]


def test_parse_command_output_nonzero_code_overrides_success_flag():
    # If a tool outputs success=True but return_code=2, exit code must force failure
    payload = {
        "success": True,
        "return_code": 2,
        "chat_output": "syntax error",
    }
    res = _parse_command_output(payload)
    assert res["success"] is False
    assert res["exit_code"] == 2
    assert res["error"] == "syntax error"


def test_parse_command_output_is_error_flag_forces_failure():
    res = _parse_command_output("unexpected exception", is_error=True)
    assert res["success"] is False
    assert res["exit_code"] == 1
    assert res["error"] == "unexpected exception"


def test_record_suggestion_command_result_success(monkeypatch):
    session_id = str(uuid.uuid4())
    incident_id = str(uuid.uuid4())

    cursor = MagicMock()
    # UPDATE incident_suggestions RETURNING incident_id
    cursor.fetchall.return_value = [(incident_id,)]

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(
        "chat.background.task.db_pool.get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )

    cmd_output = json.dumps({"success": True, "return_code": 0, "chat_output": "all good"})
    updated = _record_suggestion_command_result(session_id, "terminal_exec", cmd_output)

    assert updated is True
    # Verify suggestions update call
    update_call = cursor.execute.call_args_list[0]
    sql, params = update_call.args[0], update_call.args[1]
    assert "UPDATE incident_suggestions" in sql
    assert params[0] == "completed"
    assert params[1] == "all good"
    assert params[2] is None
    assert params[3] == 0
    assert session_id in params

    # Verify incident updated_at touched
    touch_call = cursor.execute.call_args_list[1]
    touch_sql = touch_call.args[0]
    assert "UPDATE incidents" in touch_sql
    assert "updated_at = NOW()" in touch_sql


def test_record_suggestion_command_result_failure(monkeypatch):
    session_id = str(uuid.uuid4())
    incident_id = str(uuid.uuid4())

    cursor = MagicMock()
    cursor.fetchall.return_value = [(incident_id,)]

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(
        "chat.background.task.db_pool.get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )

    cmd_output = json.dumps({"success": False, "return_code": 137, "error": "Process killed"})
    updated = _record_suggestion_command_result(session_id, "terminal_exec", cmd_output)

    assert updated is True
    update_call = cursor.execute.call_args_list[0]
    sql, params = update_call.args[0], update_call.args[1]
    assert params[0] == "failed"
    assert params[2] == "Process killed"
    assert params[3] == 137


def test_propagate_suggestion_status_preserves_existing_command_result(monkeypatch):
    """Crucial fix for #566:

    If command output was already recorded, session termination (e.g. failed
    due to worker timeout) must NEVER overwrite the command's status.
    """
    session_id = str(uuid.uuid4())

    cursor = MagicMock()
    # WHERE execution_result IS NULL finds 0 rows because result was already recorded
    cursor.fetchall.return_value = []

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(
        "chat.background.task.db_pool.get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )

    # Background session failed
    _propagate_suggestion_status(session_id, "failed")

    # Only the initial SELECT ran; NO UPDATE should be executed
    assert len(cursor.execute.call_args_list) == 1
    assert "SELECT id, incident_id, execution_status" in cursor.execute.call_args_list[0].args[0]
    assert "execution_result IS NULL" in cursor.execute.call_args_list[0].args[0]


def test_propagate_suggestion_status_recovers_tool_result_from_messages(monkeypatch):
    session_id = str(uuid.uuid4())
    incident_id = str(uuid.uuid4())

    cursor = MagicMock()
    # 1. Pending suggestion found with NULL execution_result
    cursor.fetchall.side_effect = [
        [(1, incident_id, "in_progress")],  # pending suggestions
        [(incident_id,)],                    # returned from _record_suggestion_command_result UPDATE
    ]
    # 2. SELECT messages from chat_sessions
    tool_output = json.dumps({"return_code": 0, "chat_output": "success from history"})
    cursor.fetchone.return_value = ([
        {"type": "human", "content": "run command"},
        {"type": "tool", "name": "terminal_exec", "content": tool_output},
    ],)

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(
        "chat.background.task.db_pool.get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )

    _propagate_suggestion_status(session_id, "completed")

    # Must update execution_status to 'completed' with exit code 0
    sql_calls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("UPDATE incident_suggestions" in s for s in sql_calls)


def test_mark_suggestion_executed_endpoint(monkeypatch):
    suggestion_id = 42
    incident_id = str(uuid.uuid4())
    exec_session_id = str(uuid.uuid4())

    cursor = MagicMock()
    cursor.fetchone.return_value = (suggestion_id, incident_id)
    cursor.rowcount = 1

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(
        incidents_routes.db_pool,
        "get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )
    monkeypatch.setattr(incidents_routes, "set_rls_context", lambda *a, **k: None)
    monkeypatch.setattr(incidents_routes, "get_org_id_from_request", lambda: "org-test")

    handler = incidents_routes.mark_suggestion_executed
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__

    app = Flask(__name__)
    with app.test_request_context(
        f"/api/incidents/suggestions/{suggestion_id}/mark-executed",
        method="POST",
        json={
            "chatSessionId": exec_session_id,
            "executionStatus": "in_progress",
        },
    ):
        res, status = handler("user-1", str(suggestion_id))

    assert status == 200
    assert res.get_json()["success"] is True

    # Check SQL calls
    update_sugg_call = None
    touch_incident_call = None
    link_chat_call = None

    for call_obj in cursor.execute.call_args_list:
        query = call_obj.args[0]
        if "UPDATE incident_suggestions" in query:
            update_sugg_call = call_obj
        elif "UPDATE incidents" in query:
            touch_incident_call = call_obj
        elif "UPDATE chat_sessions" in query:
            link_chat_call = call_obj

    assert update_sugg_call is not None
    assert "execution_status = %s" in update_sugg_call.args[0]
    assert "in_progress" in update_sugg_call.args[1]

    assert touch_incident_call is not None
    assert "updated_at = NOW()" in touch_incident_call.args[0]

    assert link_chat_call is not None
    assert "incident_id = %s" in link_chat_call.args[0]
