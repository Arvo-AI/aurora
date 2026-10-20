"""ServiceNow write tools refuse unattended posts when write-back is off, and
create/update also need the "full" write scope."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_import_stubs() -> None:
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)
    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                return fn
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    sys.modules.setdefault("celery_config", celery_cfg)


_install_import_stubs()

from chat.backend.agent.tools import servicenow_tool as tool  # noqa: E402

SYS_ID = "f" * 32
_RCA_SESSION = SimpleNamespace(is_background=True, incident_id="inc-1", rca_context={"source": "servicenow"})
_CHAT_SESSION = SimpleNamespace(is_background=False, incident_id=None, rca_context=None)
# An unattended Action: background, but not an RCA (no incident, source "action").
_ACTION_SESSION = SimpleNamespace(is_background=True, incident_id=None, rca_context={"source": "action"})


def _set_session(monkeypatch, state):
    monkeypatch.setattr("utils.cloud.cloud_utils.get_state_context", lambda: state)


@pytest.fixture
def prefs(monkeypatch):
    values = {"work_notes": False, "write_mode": "notes_only"}
    monkeypatch.setattr("routes.servicenow.tasks.work_notes_enabled", lambda user_id: values["work_notes"])
    monkeypatch.setattr("routes.servicenow.tasks.get_write_mode", lambda user_id: values["write_mode"])
    return values


@pytest.fixture
def client(monkeypatch):
    fake = MagicMock()
    fake.instance_url = "https://acme.service-now.com"
    fake.get_incident.return_value = {"sys_id": SYS_ID, "number": "INC0010001"}
    fake.add_work_note.return_value = {"sys_id": SYS_ID, "number": "INC0010001"}
    fake.create_incident.return_value = {"sys_id": SYS_ID, "number": "INC0010002", "state": "New", "priority": "2 - High"}
    fake.update_incident.return_value = {"sys_id": SYS_ID, "number": "INC0010001", "state": "In Progress"}
    creds = {"instance_url": fake.instance_url, "auth_type": "basic", "capabilities": {"can_write_incidents": True}}
    monkeypatch.setattr(tool, "get_token_data", lambda user_id, provider: dict(creds))
    monkeypatch.setattr(tool.ServiceNowClient, "from_credentials", classmethod(lambda cls, c: fake))
    fake.creds = creds
    return fake


@pytest.mark.parametrize("fn,args", [
    (tool.servicenow_add_work_note, ("INC0010001", "hello")),
    (tool.servicenow_create_incident, ("Checkout down",)),
    (tool.servicenow_update_incident, ("INC0010001", {"state": "in_progress"})),
])
@pytest.mark.parametrize(
    "state", [None, _RCA_SESSION, _CHAT_SESSION, _ACTION_SESSION],
    ids=["no-session", "background-rca", "interactive-chat", "background-action"],
)
def test_write_tools_blocked_when_write_back_off(monkeypatch, prefs, client, fn, args, state):
    _set_session(monkeypatch, state)
    with pytest.raises(ValueError, match="write-back is disabled"):
        fn(*args, user_id="uid-1")
    client.add_work_note.assert_not_called()
    client.create_incident.assert_not_called()
    client.update_incident.assert_not_called()


def test_chat_requests_write_once_the_switch_is_on(monkeypatch, prefs, client):
    """The connector-page switch is the single control: a chat request needs it too, and is enough."""
    prefs["work_notes"] = True
    _set_session(monkeypatch, _CHAT_SESSION)
    out = json.loads(tool.servicenow_add_work_note("INC0010001", "hello", user_id="uid-1"))
    assert out["status"] == "success" and out["sys_id"] == SYS_ID
    client.add_work_note.assert_called_once_with(SYS_ID, "hello", customer_visible=False)


@pytest.mark.parametrize("fn,args,method", [
    (tool.servicenow_add_work_note, ("INC0010001", "hello"), "add_work_note"),
    (tool.servicenow_create_incident, ("Checkout down",), "create_incident"),
    (tool.servicenow_update_incident, ("INC0010001", {"state": "in_progress"}), "update_incident"),
])
def test_a_403_downgrades_the_stored_write_capability(monkeypatch, prefs, client, fn, args, method):
    """A forbidden account must not be retried on every later call: the capability is cleared, like the work-note service does."""
    prefs.update(work_notes=True, write_mode="full")
    _set_session(monkeypatch, _RCA_SESSION)
    getattr(client, method).side_effect = tool.ServiceNowAPIError("Insufficient rights", 403)
    disabled = MagicMock()
    monkeypatch.setattr("utils.notifications.servicenow_notification_service.disable_write_capability", disabled)
    out = json.loads(fn(*args, user_id="uid-1"))
    assert "error" in out
    disabled.assert_called_once()
    assert disabled.call_args[0][0] == "uid-1"
    assert disabled.call_args[0][1]["instance_url"] == client.instance_url


def test_a_non_403_failure_keeps_the_write_capability(monkeypatch, prefs, client):
    prefs["work_notes"] = True
    _set_session(monkeypatch, _RCA_SESSION)
    client.add_work_note.side_effect = tool.ServiceNowAPIError("gateway", 502)
    disabled = MagicMock()
    monkeypatch.setattr("utils.notifications.servicenow_notification_service.disable_write_capability", disabled)
    out = json.loads(tool.servicenow_add_work_note("INC0010001", "hello", user_id="uid-1"))
    assert "error" in out
    disabled.assert_not_called()


def test_rca_note_allowed_once_opted_in(monkeypatch, prefs, client):
    prefs["work_notes"] = True
    _set_session(monkeypatch, _RCA_SESSION)
    out = json.loads(tool.servicenow_add_work_note(SYS_ID, "root cause", customer_visible=True, user_id="uid-1"))
    assert out["field"] == "comments"
    client.add_work_note.assert_called_once_with(SYS_ID, "root cause", customer_visible=True)


@pytest.mark.parametrize("fn,args", [
    (tool.servicenow_create_incident, ("Checkout down",)),
    (tool.servicenow_update_incident, ("INC0010001", {"state": "in_progress"})),
])
def test_create_and_update_need_full_scope(monkeypatch, prefs, client, fn, args):
    prefs["work_notes"] = True
    _set_session(monkeypatch, _RCA_SESSION)
    with pytest.raises(ValueError, match="work notes only"):
        fn(*args, user_id="uid-1")


def test_full_scope_create_and_update(monkeypatch, prefs, client):
    prefs.update(work_notes=True, write_mode="full")
    _set_session(monkeypatch, _RCA_SESSION)
    created = json.loads(tool.servicenow_create_incident(
        "Checkout down", description="500s", priority=2, assignment_group="Platform", user_id="uid-1",
    ))
    assert created["number"] == "INC0010002" and created["url"].endswith(f"sys_id={SYS_ID}")
    client.create_incident.assert_called_once()
    body = client.create_incident.call_args[0][0]
    assert body["priority"] == "2" and body["assignment_group"] == "Platform" and body["caller_id"] is None

    updated = json.loads(tool.servicenow_update_incident(
        "INC0010001", {"state": "Resolved", "close_code": "Solution provided", "close_notes": "fixed", "sys_id": "nope"},
        user_id="uid-1",
    ))
    assert updated["updated"] == ["close_code", "close_notes", "state"]
    client.update_incident.assert_called_once_with(
        SYS_ID, {"state": "6", "close_code": "Solution provided", "close_notes": "fixed"},
    )


def test_update_rejects_unknown_state_and_empty_fields(monkeypatch, prefs, client):
    prefs.update(work_notes=True, write_mode="full")
    _set_session(monkeypatch, _CHAT_SESSION)
    with pytest.raises(ValueError, match="Unsupported value for state"):
        tool.servicenow_update_incident("INC0010001", {"state": "done"}, user_id="uid-1")
    with pytest.raises(ValueError, match="No supported fields"):
        tool.servicenow_update_incident("INC0010001", {"sys_id": "x"}, user_id="uid-1")


def test_writes_refused_when_credentials_lost_write_capability(monkeypatch, prefs, client):
    prefs["work_notes"] = True
    client.creds["capabilities"] = {"can_write_incidents": False}
    _set_session(monkeypatch, _RCA_SESSION)
    out = json.loads(tool.servicenow_add_work_note("INC0010001", "hello", user_id="uid-1"))
    assert "cannot write incidents" in out["error"]
    client.add_work_note.assert_not_called()


def test_unknown_incident_number_reports_not_found(monkeypatch, prefs, client):
    prefs["work_notes"] = True
    client.get_incident.return_value = None
    _set_session(monkeypatch, _RCA_SESSION)
    out = json.loads(tool.servicenow_add_work_note("INC0099999", "hello", user_id="uid-1"))
    assert "not found" in out["error"]
