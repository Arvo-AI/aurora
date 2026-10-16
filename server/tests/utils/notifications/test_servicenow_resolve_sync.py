"""Resolution sync (Aurora → ServiceNow): opt-in, full scope only, 403 disables writes (no DB, no network)."""

import importlib
from unittest.mock import MagicMock

import pytest

from routes.servicenow.servicenow_client import ServiceNowAPIError
from utils.notifications import servicenow_notification_service as svc

SYS_ID = "e" * 32
SUMMARY = (
    "What happened: the API tier fell over.\n\nRoot cause: the connection pool was exhausted after a "
    "deploy doubled the worker count without raising the Postgres max_connections limit."
)


@pytest.fixture
def wired(monkeypatch):
    prefs = {"work_notes": True, "write_mode": "full", "resolve": {"enabled": True, "closeCode": "Solution provided"}}
    # Patch the module object, not a dotted string: tests/auth evicts routes.* from
    # sys.modules, after which a string path resolves through the stale `routes`
    # package while the service imports a fresh tasks module at call time.
    tasks = importlib.import_module("routes.servicenow.tasks")
    monkeypatch.setattr(tasks, "work_notes_enabled", lambda user_id: prefs["work_notes"])
    monkeypatch.setattr(tasks, "get_write_mode", lambda user_id: prefs["write_mode"])
    monkeypatch.setattr(tasks, "get_resolve_sync", lambda user_id: prefs["resolve"])
    incident = {"source_type": "servicenow", "alert_metadata": {"sysId": SYS_ID}, "aurora_summary": SUMMARY}
    monkeypatch.setattr(svc, "_load_incident", lambda user_id, incident_id: incident)
    creds = {"instance_url": "https://acme.service-now.com", "auth_type": "basic",
             "capabilities": {"can_write_incidents": True}}
    client = MagicMock()
    client.update_incident.return_value = {"sys_id": SYS_ID, "state": "Resolved"}
    writes = []
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: dict(creds))
    monkeypatch.setattr(svc, "store_tokens_in_db", lambda user_id, data, provider: writes.append(data))
    monkeypatch.setattr(svc.ServiceNowClient, "from_credentials", classmethod(lambda cls, c: client))
    return prefs, incident, client, writes


def test_resolves_with_close_code_and_root_cause(wired):
    prefs, incident, client, writes = wired
    assert svc.sync_servicenow_resolution("u1", "i1") is True
    sys_id, body = client.update_incident.call_args[0]
    assert sys_id == SYS_ID
    assert body["state"] == "6"
    assert body["close_code"] == "Solution provided"
    assert "connection pool was exhausted" in body["close_notes"]
    assert writes == []


@pytest.mark.parametrize("change", [
    {"work_notes": False}, {"write_mode": "notes_only"}, {"resolve": {"enabled": False, "closeCode": "x"}},
])
def test_skipped_unless_every_opt_in_is_on(wired, change):
    prefs, incident, client, writes = wired
    prefs.update(change)
    assert svc.sync_servicenow_resolution("u1", "i1") is False
    client.update_incident.assert_not_called()


def test_skipped_for_other_sources_and_missing_sys_id(wired):
    prefs, incident, client, writes = wired
    incident["source_type"] = "jira"
    assert svc.sync_servicenow_resolution("u1", "i1") is False
    incident["source_type"] = "servicenow"
    incident["alert_metadata"] = {}
    assert svc.sync_servicenow_resolution("u1", "i1") is False
    client.update_incident.assert_not_called()


def test_short_summary_still_resolves_with_a_link_note(wired):
    prefs, incident, client, writes = wired
    incident["aurora_summary"] = "short"
    assert svc.sync_servicenow_resolution("u1", "i1") is True
    assert "Resolved in Aurora" in client.update_incident.call_args[0][1]["close_notes"]


def test_forbidden_disables_write_capability(wired):
    prefs, incident, client, writes = wired
    client.update_incident.side_effect = ServiceNowAPIError("forbidden", 403)
    assert svc.sync_servicenow_resolution("u1", "i1") is False
    assert writes and writes[0]["capabilities"]["can_write_incidents"] is False


def test_other_errors_never_raise(wired):
    prefs, incident, client, writes = wired
    client.update_incident.side_effect = ServiceNowAPIError("timeout", None)
    assert svc.sync_servicenow_resolution("u1", "i1") is False
    assert writes == []
