"""Manual VMs must be org-scoped on write, and Save must not drop a good check."""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# The linter job does not install paramiko. The route imports it through
# ssh_utils, and this file never opens a real SSH session.
try:
    __import__("paramiko")
except ModuleNotFoundError:
    sys.modules["paramiko"] = MagicMock()

import pytest
from flask import Flask

import routes.vms.manual_vms_routes as vms

ORG = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"


def _unwrap(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _call(handler, *args):
    result = handler(*args)
    # 200 responses are a bare Response; created rows are (response, 201)
    if isinstance(result, tuple):
        return result
    return result, result.status_code


def _sql_text(query) -> str:
    if isinstance(query, str):
        return query
    return query.as_string(None)


def _pool(cursor):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    pool = MagicMock()
    pool.get_user_connection.return_value.__enter__.return_value = conn
    pool.get_user_connection.return_value.__exit__.return_value = False
    return pool


def _row(verified=True):
    now = datetime(2026, 1, 1)
    return (1, "web", "203.0.113.10", 22, None, 7, "ubuntu", verified, now, now)


@pytest.fixture
def routes(monkeypatch):
    cursor = MagicMock()
    cursor.rowcount = 1
    monkeypatch.setattr(vms, "db_pool", _pool(cursor))
    monkeypatch.setattr(vms, "get_org_id_from_request", lambda: ORG)
    monkeypatch.setattr(vms, "set_rls_context", lambda *a, **k: k.get("org_id") or ORG)
    monkeypatch.setattr(vms, "parse_ssh_key_id", lambda value: (int(value), None))
    monkeypatch.setattr(vms, "load_user_private_key_safe", lambda *a, **k: ("pem", None))
    monkeypatch.setattr(vms, "validate_and_test_ssh", lambda *a, **k: (True, None, "ubuntu"))
    return cursor


def test_unchanged_ssh_target_does_not_count_as_a_change():
    stored = {
        "ip_address": "203.0.113.10",
        "port": 22,
        "ssh_jump_command": None,
        "ssh_key_id": 7,
        "ssh_username": "ubuntu",
    }
    # Save resubmits blanks and strings for values that are already stored
    proposed = {
        "ip_address": "203.0.113.10",
        "port": "22",
        "ssh_jump_command": "  ",
        "ssh_key_id": 7,
        "ssh_username": "ubuntu",
    }
    assert vms.connection_fields_changed(stored, proposed) is False


def test_a_different_jump_host_invalidates_the_check():
    stored = {"ssh_jump_command": None, "ssh_key_id": 7}
    assert vms.connection_fields_changed(
        stored, {"ssh_jump_command": "ssh -J bastion"}
    ) is True


def test_omitted_fields_do_not_invalidate_the_check():
    stored = {"ip_address": "203.0.113.10", "ssh_key_id": 7}
    assert vms.connection_fields_changed(stored, {}) is False


def test_create_stamps_org_id_and_keeps_a_matching_verification(routes):
    routes.fetchone.return_value = _row(verified=True)
    app = Flask(__name__)
    handler = _unwrap(vms.create_manual_vm)
    payload = {
        "name": "web",
        "ipAddress": "203.0.113.10",
        "port": 22,
        "sshKeyId": 7,
        "sshUsername": "ubuntu",
    }
    with app.test_request_context("/api/vms/manual", method="POST", json=payload):
        response, status = _call(handler, USER)

    assert status == 201
    sql = _sql_text(routes.execute.call_args.args[0])
    params = routes.execute.call_args.args[1]
    assert "INSERT INTO user_manual_vms (user_id, org_id," in sql
    assert "ELSE user_manual_vms.connection_verified" in sql
    assert "connection_verified = FALSE" not in sql
    assert params[1] == ORG
    assert response.get_json()["connectionVerified"] is True


def test_create_without_an_org_does_not_insert(routes, monkeypatch):
    monkeypatch.setattr(vms, "set_rls_context", lambda *a, **k: None)
    app = Flask(__name__)
    handler = _unwrap(vms.create_manual_vm)
    with app.test_request_context(
        "/api/vms/manual",
        method="POST",
        json={"name": "web", "ipAddress": "203.0.113.10"},
    ):
        response, status = _call(handler, USER)

    assert status == 400
    assert response.get_json()["error"] == "Organization context required"
    routes.execute.assert_not_called()


def _put(routes, payload, stored_user=USER):
    routes.fetchone.side_effect = [
        (stored_user, "203.0.113.10", 22, None, 7, "ubuntu"),
        _row(verified=True),
    ]
    app = Flask(__name__)
    handler = _unwrap(vms.update_manual_vm)
    with app.test_request_context("/api/vms/manual/1", method="PUT", json=payload):
        response, status = _call(handler, USER, 1)
    update = next(
        call for call in routes.execute.call_args_list if "UPDATE" in _sql_text(call.args[0])
    )
    return response, status, _sql_text(update.args[0]), update.args[1]


def test_save_of_the_same_ssh_target_keeps_verified_and_stamps_org(routes):
    _, status, sql, params = _put(
        routes,
        {
            "name": "web",
            "ipAddress": "203.0.113.10",
            "port": 22,
            "sshJumpCommand": None,
            "sshKeyId": 7,
            "sshUsername": "ubuntu",
        },
    )

    assert status == 200
    set_clause = sql.split("WHERE", 1)[0]
    assert "connection_verified = FALSE" not in set_clause
    assert "org_id = %s" in set_clause
    assert ORG in params


def test_save_of_a_new_address_clears_verified(routes):
    _, status, sql, _params = _put(
        routes,
        {
            "name": "web",
            "ipAddress": "203.0.113.11",
            "port": 22,
            "sshKeyId": 7,
            "sshUsername": "ubuntu",
        },
    )

    assert status == 200
    assert "connection_verified = FALSE" in sql.split("WHERE", 1)[0]


def test_connection_check_stamps_org_id_when_it_marks_ready(routes):
    routes.fetchone.return_value = ("203.0.113.10", 22, None, 7, "ubuntu", USER)
    app = Flask(__name__)
    handler = _unwrap(vms.check_manual_vm_connection)
    with app.test_request_context(
        "/api/vms/check-connection",
        method="POST",
        json={"vmId": 1, "username": "ubuntu"},
    ):
        response, status = _call(handler, USER)

    assert status == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["connectionVerified"] is True
    updates = [
        _sql_text(call.args[0])
        for call in routes.execute.call_args_list
        if "UPDATE" in _sql_text(call.args[0])
    ]
    assert len(updates) == 1
    assert "connection_verified = TRUE" in updates[0]
    assert "org_id = %s" in updates[0]
    assert "IS NOT DISTINCT FROM" in updates[0]
    verify_params = next(
        call.args[1]
        for call in routes.execute.call_args_list
        if "UPDATE" in _sql_text(call.args[0])
    )
    assert verify_params[0] == ORG


def test_check_with_a_different_user_does_not_mark_the_saved_vm_ready(routes):
    routes.fetchone.return_value = ("203.0.113.10", 22, None, 7, "ubuntu", USER)
    app = Flask(__name__)
    handler = _unwrap(vms.check_manual_vm_connection)
    with app.test_request_context(
        "/api/vms/check-connection",
        method="POST",
        json={"vmId": 1, "username": "root", "sshKeyId": 7},
    ):
        response, status = _call(handler, USER)

    assert status == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["connectionVerified"] is False
    updates = [
        _sql_text(call.args[0])
        for call in routes.execute.call_args_list
        if "UPDATE" in _sql_text(call.args[0])
    ]
    assert updates == []
