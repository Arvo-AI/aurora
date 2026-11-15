"""get_alert_field: ServiceNow payloads are found by the record's sys_id, not by a row id."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "chat" / "backend" / "agent" / "tools" / "alert_payload_tool.py"
_spec = importlib.util.spec_from_file_location("alert_payload_tool_under_test", _PATH)
alert_payload_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alert_payload_tool)  # by path: the tools package __init__ pulls in every connector


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return []


@pytest.fixture
def rls(monkeypatch):
    auth_mod = ModuleType("utils.auth.stateless_auth")
    auth_mod.set_rls_context = lambda *a, **k: "org-1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils.auth.stateless_auth", auth_mod)


def test_servicenow_payload_is_matched_on_sys_id(rls):
    cursor = _Cursor([("servicenow", 123456789, None), ({"number": "INC0010001"},)])
    payload, error = alert_payload_tool._fetch_payload(cursor, None, "inc-1", "user-1")
    assert error is None
    assert payload == {"number": "INC0010001"}
    sql, params = cursor.executed[1]
    assert "FROM servicenow_events e" in sql
    assert "e.sys_id = i.alert_metadata->>'sysId'" in sql
    assert "ORDER BY e.received_at DESC LIMIT 1" in sql
    assert params == ("inc-1", "user-1")


def test_other_sources_keep_the_row_id_lookup(rls):
    cursor = _Cursor([("grafana", 42, None), ({"alerts": []},)])
    payload, error = alert_payload_tool._fetch_payload(cursor, None, "inc-1", "user-1")
    assert error is None
    assert payload == {"alerts": []}
    sql, params = cursor.executed[1]
    assert "FROM grafana_alerts WHERE id = %s" in sql
    assert params == (42,)
