"""servicenow_notification_service: eligibility, claim-before-post, error handling (no DB, no network)."""

from unittest.mock import MagicMock

import pytest

from routes.servicenow.servicenow_client import ServiceNowAPIError
from utils.notifications import servicenow_notification_service as svc

SYS_ID = "d" * 32
ROOT_CAUSE = (
    "Root cause: the connection pool was exhausted after a deploy doubled the worker count "
    "without raising the Postgres max_connections limit."
)
SUMMARY = "What happened: the API tier fell over during the 14:00 deploy.\n\n" + ROOT_CAUSE


def _anchor(**over):
    data = {
        "incident_id": "i1", "source_type": "servicenow", "recurrence_of": None, "servicenow_note_id": None,
        "alert_metadata": {"sysId": SYS_ID, "number": "INC0010001"}, "aurora_summary": SUMMARY,
    }
    data.update(over)
    return data


class _Claims:
    def __init__(self, claim_ok=True):
        self.claim_ok = claim_ok
        self.claimed, self.released, self.recorded = [], [], []

    def claim(self, incident_id, user_id, token="pending"):
        self.claimed.append(incident_id)
        return self.claim_ok

    def release(self, incident_id, user_id, token="pending"):
        self.released.append(incident_id)

    def record(self, incident_id, user_id, remote_id):
        self.recorded.append((incident_id, remote_id))


@pytest.fixture
def wired(monkeypatch):
    """Stored creds, a fake client and a fake claim table; returns (claims, client, stored_writes)."""
    creds = {"instance_url": "https://acme.service-now.com", "auth_type": "basic",
             "username": "aurora", "password": "pw", "capabilities": {"can_write_incidents": True}}
    client = MagicMock()
    client.add_work_note.return_value = {"sys_id": SYS_ID, "sys_updated_on": "2026-10-08 21:30:00"}
    claims = _Claims()
    writes = []
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: dict(creds))
    monkeypatch.setattr(svc, "store_tokens_in_db", lambda user_id, data, provider: writes.append(data))
    monkeypatch.setattr(svc.ServiceNowClient, "from_credentials", classmethod(lambda cls, c: client))
    monkeypatch.setattr(svc, "_claims", claims)
    return claims, client, writes


@pytest.mark.parametrize("over, reason", [
    ({"source_type": "pagerduty"}, "not a ServiceNow"),
    ({"recurrence_of": "root-1"}, "recurrence of root-1"),
    ({"servicenow_note_id": "posted"}, "already posted"),
    ({"servicenow_note_id": "pending"}, "already posted"),
    ({"alert_metadata": {"number": "INC1"}}, "no ServiceNow sys_id"),
    ({"alert_metadata": {"sysId": "../incident"}}, "no ServiceNow sys_id"),
    ({"alert_metadata": None}, "no ServiceNow sys_id"),
    ({"aurora_summary": "Short summary."}, "too short"),
    ({"aurora_summary": None}, "too short"),
])
def test_should_post_rejects(over, reason):
    ok, why = svc._should_post(_anchor(**over))
    assert ok is False
    assert reason in why


def test_should_post_accepts_a_well_formed_anchor():
    assert svc._should_post(_anchor()) == (True, "")


def test_should_post_decodes_string_alert_metadata():
    assert svc._should_post(_anchor(alert_metadata='{"sysId": "%s"}' % SYS_ID))[0] is True


def test_posts_once_and_records_the_marker(wired):
    claims, client, _ = wired
    assert svc.send_servicenow_work_note("u1", _anchor()) is True
    client.add_work_note.assert_called_once()
    sys_id, content = client.add_work_note.call_args[0]
    assert sys_id == SYS_ID
    assert "connection pool was exhausted" in content
    assert "Root cause: the connection" not in content  # inline label dropped by extract_note_body
    assert claims.claimed == ["i1"]
    assert claims.recorded == [("i1", "2026-10-08 21:30:00")]
    assert claims.released == []


def test_skips_when_claim_is_taken(wired):
    claims, client, _ = wired
    claims.claim_ok = False
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    client.add_work_note.assert_not_called()


def test_skips_when_not_connected(wired, monkeypatch):
    _, client, _ = wired
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: None)
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    client.add_work_note.assert_not_called()


def test_skips_when_write_capability_was_disabled(wired, monkeypatch):
    _, client, _ = wired
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: {
        "instance_url": "https://acme.service-now.com", "capabilities": {"can_write_incidents": False}})
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    client.add_work_note.assert_not_called()


def test_forbidden_releases_the_claim_and_disables_writes(wired):
    claims, client, writes = wired
    client.add_work_note.side_effect = ServiceNowAPIError("forbidden", 403)
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    assert claims.released == ["i1"]
    assert claims.recorded == []
    assert writes
    assert writes[-1]["capabilities"]["can_write_incidents"] is False


def test_other_4xx_releases_the_claim_but_keeps_the_capability(wired):
    claims, client, writes = wired
    client.add_work_note.side_effect = ServiceNowAPIError("not found", 404)
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    assert claims.released == ["i1"]
    assert writes == []


@pytest.mark.parametrize("status", [None, 500, 502])
def test_unknown_outcome_keeps_the_claim(wired, status):
    claims, client, _ = wired
    client.add_work_note.side_effect = ServiceNowAPIError("gateway", status)
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
    assert claims.released == []
    assert claims.recorded == []


def test_never_raises(wired, monkeypatch):
    monkeypatch.setattr(svc, "get_token_data", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert svc.send_servicenow_work_note("u1", _anchor()) is False
