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
from unittest.mock import MagicMock

import pytest
from flask import Flask

from chat.background.task import (
    _parse_command_output,
    _record_suggestion_command_result,
    _propagate_suggestion_status,
)
import routes.incidents_routes as incidents_routes


@pytest.fixture
def mock_task_db(monkeypatch):
    """Provide a mock DB connection and cursor for chat.background.task functions."""
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(
        "chat.background.task.db_pool.get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )
    return cursor, conn


@pytest.fixture
def mock_routes_db(monkeypatch):
    """Provide a mock DB connection and cursor for incidents_routes endpoints."""
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(
        incidents_routes.db_pool,
        "get_admin_connection",
        MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=conn))),
    )
    monkeypatch.setattr(incidents_routes, "set_rls_context", lambda *a, **k: None)
    monkeypatch.setattr(incidents_routes, "get_org_id_from_request", lambda: "org-test")
    return cursor, conn


@pytest.fixture
def route_client():
    """Unwrap mark_suggestion_executed route handler and return a test request runner."""
    handler = incidents_routes.mark_suggestion_executed
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__

    app = Flask(__name__)

    def _call(suggestion_id: str, payload: dict):
        with app.test_request_context(
            f"/api/incidents/suggestions/{suggestion_id}/mark-executed",
            method="POST",
            json=payload,
        ):
            return handler("user-1", suggestion_id)

    return _call


@pytest.mark.parametrize(
    "raw_input, is_err, expected_success, expected_code, expected_out, expected_err",
    [
        (
            json.dumps({
                "success": True,
                "command": "kubectl rollout restart deployment/api",
                "return_code": 0,
                "chat_output": "deployment.apps/api restarted",
            }),
            False,
            True,
            0,
            "deployment.apps/api restarted",
            None,
        ),
        (
            json.dumps({
                "success": False,
                "command": "kubectl rollout restart deployment/nonexistent",
                "return_code": 1,
                "error": 'Error from server (NotFound): deployments.apps "nonexistent" not found',
            }),
            False,
            False,
            1,
            None,
            'Error from server (NotFound): deployments.apps "nonexistent" not found',
        ),
        (
            {"success": True, "return_code": 2, "chat_output": "syntax error"},
            False,
            False,
            2,
            None,
            "syntax error",
        ),
        (
            "unexpected exception",
            True,
            False,
            1,
            None,
            "unexpected exception",
        ),
    ],
)
def test_parse_command_output_variations(
    raw_input, is_err, expected_success, expected_code, expected_out, expected_err
):
    """Verify command output parsing across exit codes, JSON formats, and error overrides."""
    res = _parse_command_output(raw_input, is_error=is_err)
    assert res["success"] is expected_success
    assert res["exit_code"] == expected_code
    if expected_out is not None:
        assert res["output"] == expected_out
    if expected_err is not None:
        assert expected_err in (res["error"] or "")


def test_parse_command_output_truncates_large_payload():
    """Verify stdout strings exceeding MAX_SUGGESTION_OUTPUT_CHARS are truncated to 10 KiB."""
    large_stdout = "x" * 20000
    payload = json.dumps({
        "success": True,
        "return_code": 0,
        "chat_output": large_stdout,
    })
    res = _parse_command_output(payload)
    assert res["success"] is True
    assert len(res["output"]) == 10240
    assert res["output"] == "x" * 10240


def test_record_suggestion_command_result_success(mock_task_db):
    """Verify recording a successful command updates status to completed with exit code 0."""
    cursor, _ = mock_task_db
    session_id, incident_id = str(uuid.uuid4()), str(uuid.uuid4())
    cursor.fetchall.return_value = [(incident_id,)]

    cmd_output = json.dumps({"success": True, "return_code": 0, "chat_output": "all good"})
    updated = _record_suggestion_command_result(session_id, "terminal_exec", cmd_output)

    assert updated is True
    update_call = cursor.execute.call_args_list[0]
    sql, params = update_call.args[0], update_call.args[1]
    assert "UPDATE incident_suggestions" in sql
    assert params[0] == "completed"
    assert params[1] == "all good"
    assert params[2] is None
    assert params[3] == 0
    assert session_id in params

    touch_call = cursor.execute.call_args_list[1]
    assert "UPDATE incidents" in touch_call.args[0]
    assert "updated_at = NOW()" in touch_call.args[0]


def test_record_suggestion_command_result_failure(mock_task_db):
    """Verify recording a failing command updates status to failed with non-zero exit code."""
    cursor, _ = mock_task_db
    session_id, incident_id = str(uuid.uuid4()), str(uuid.uuid4())
    cursor.fetchall.return_value = [(incident_id,)]

    cmd_output = json.dumps({"success": False, "return_code": 137, "error": "Process killed"})
    updated = _record_suggestion_command_result(session_id, "terminal_exec", cmd_output)

    assert updated is True
    update_call = cursor.execute.call_args_list[0]
    sql, params = update_call.args[0], update_call.args[1]
    assert params[0] == "failed"
    assert params[2] == "Process killed"
    assert params[3] == 137


def test_record_suggestion_command_result_ignores_non_command_tools():
    """Verify non-command tools (e.g. memory_tool, artifact_tool) are skipped."""
    session_id = str(uuid.uuid4())
    assert _record_suggestion_command_result(session_id, "memory_tool", {"status": "saved"}) is False
    assert _record_suggestion_command_result(session_id, "artifact_tool", "saved to disk") is False


def test_record_suggestion_command_result_status_guard(mock_task_db):
    """Verify status guard only updates suggestions still in in_progress or executed state."""
    cursor, _ = mock_task_db
    session_id, incident_id = str(uuid.uuid4()), str(uuid.uuid4())
    cursor.fetchall.return_value = [(incident_id,)]

    cmd_output = json.dumps({"success": True, "return_code": 0, "chat_output": "restarted"})
    _record_suggestion_command_result(session_id, "terminal_exec", cmd_output)

    update_sql = cursor.execute.call_args_list[0].args[0]
    assert "execution_status IN ('in_progress', 'executed')" in update_sql


def test_propagate_suggestion_status_preserves_existing_command_result(mock_task_db):
    """Verify background session termination NEVER overwrites an already-recorded command result."""
    cursor, _ = mock_task_db
    session_id = str(uuid.uuid4())
    cursor.fetchall.return_value = []

    _propagate_suggestion_status(session_id, "failed")

    assert len(cursor.execute.call_args_list) == 1
    select_sql = cursor.execute.call_args_list[0].args[0]
    assert "SELECT id, incident_id, execution_status" in select_sql
    assert "execution_result IS NULL" in select_sql


def test_propagate_suggestion_status_recovers_tool_result_from_messages(mock_task_db):
    """Verify tool output is recovered from chat_sessions messages if execution_result was NULL."""
    cursor, _ = mock_task_db
    session_id, incident_id = str(uuid.uuid4()), str(uuid.uuid4())

    cursor.fetchall.side_effect = [
        [(1, incident_id, "in_progress")],
        [(incident_id,)],
    ]
    tool_output = json.dumps({"return_code": 0, "chat_output": "success from history"})
    cursor.fetchone.return_value = ([
        {"type": "human", "content": "run command"},
        {"type": "tool", "name": "terminal_exec", "content": tool_output},
    ],)

    _propagate_suggestion_status(session_id, "completed")

    sql_calls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("UPDATE incident_suggestions" in s for s in sql_calls)


def test_mark_suggestion_executed_endpoint(mock_routes_db, route_client):
    """Verify mark_suggestion_executed updates suggestion status, incident timestamp, and links chat."""
    cursor, _ = mock_routes_db
    suggestion_id = 42
    incident_id = str(uuid.uuid4())
    exec_session_id = str(uuid.uuid4())

    cursor.fetchone.return_value = (suggestion_id, incident_id)
    cursor.rowcount = 1

    res, status = route_client(
        str(suggestion_id),
        {
            "chatSessionId": exec_session_id,
            "executionStatus": "in_progress",
        },
    )

    assert status == 200
    assert res.get_json()["success"] is True

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


@pytest.mark.parametrize(
    "payload, expected_err",
    [
        ({"executionStatus": "hacked"}, "Invalid executionStatus"),
        ({"executionExitCode": "not-an-int"}, "Invalid executionExitCode"),
        ({"executionResult": ["not", "a", "dict"]}, "Invalid executionResult"),
        ({"chatSessionId": "not-a-valid-uuid"}, "Invalid chatSessionId"),
    ],
)
def test_mark_suggestion_executed_validation_errors(route_client, payload, expected_err):
    """Verify mark_suggestion_executed rejects invalid input params with 400 Bad Request."""
    res, status = route_client("42", payload)
    assert status == 400
    assert expected_err in res.get_json()["error"]


def test_mark_suggestion_executed_reexecution_resets_prior_outputs(mock_routes_db, route_client):
    """Verify re-executing a suggestion clears prior execution output, error, exit code, and result."""
    cursor, _ = mock_routes_db
    suggestion_id = 99
    incident_id = str(uuid.uuid4())
    exec_session_id = str(uuid.uuid4())

    cursor.fetchone.return_value = (suggestion_id, incident_id)
    cursor.rowcount = 1

    res, status = route_client(
        str(suggestion_id),
        {
            "chatSessionId": exec_session_id,
            "executionStatus": "in_progress",
        },
    )

    assert status == 200
    update_sugg_sql = None
    for call_obj in cursor.execute.call_args_list:
        if "UPDATE incident_suggestions" in call_obj.args[0]:
            update_sugg_sql = call_obj.args[0]
            break

    assert update_sugg_sql is not None
    assert "execution_output = NULL" in update_sugg_sql
    assert "execution_error = NULL" in update_sugg_sql
    assert "execution_exit_code = NULL" in update_sugg_sql
    assert "execution_result = NULL" in update_sugg_sql
