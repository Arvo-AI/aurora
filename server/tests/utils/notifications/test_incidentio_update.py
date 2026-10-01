"""incidentio_notification_service: post the RCA back as an incident.io incident update (no DB, no network)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.incidentio.incidentio_client import IncidentioAPIError, IncidentioClient
from utils.notifications import incidentio_notification_service as svc
from utils.notifications import postback_claim

ROOT_CAUSE = (
    "Root cause: the connection pool was exhausted after a deploy doubled the worker count "
    "without raising the Postgres max_connections limit."
)
SUMMARY = "What happened: the API tier fell over during the 14:00 deploy.\n\n" + ROOT_CAUSE
# What the note shows: the summarizer's inline "Root cause:" label is dropped
ROOT_CAUSE_BODY = ROOT_CAUSE[len("Root cause: "):]

# The stored webhook payloads an Aurora incident can originate from
INCIDENT_EVENT = {
    "event_type": "public_incident.incident_created_v2",
    "event": {"incident": {"id": "inc_1", "name": "Database is sad", "status": "triage"}},
}
ALERT_EVENT = {
    "event_type": "public_alert.alert_created_v1",
    "event": {"alert": {"id": "alert_1", "title": "High CPU on api", "status": "firing"}},
}

CLAIM = ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s AND incidentio_update_id IS NULL", ("pending", "i1"))
RELEASE = ("UPDATE incidents SET incidentio_update_id = NULL WHERE id = %s AND incidentio_update_id = %s", ("i1", "pending"))
RECORD = ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s", ("upd_1", "i1"))


def _anchor(**over):
    data = {
        "incident_id": "i1", "source_type": "incidentio", "recurrence_of": None,
        "incidentio_update_id": None, "source_alert_id": 42, "aurora_summary": SUMMARY,
    }
    data.update(over)
    return data


def _attached(*incidents):
    return {"incident_alerts": [{"incident": inc} for inc in incidents]}


@pytest.fixture
def wired(monkeypatch, patched_db):
    client = MagicMock(name="client")
    client.post_incident_update.return_value = {"incident_update": {"id": "upd_1"}}
    client.list_incident_alerts.return_value = _attached()
    built = []

    def fake_client(api_key):
        built.append(api_key)
        return client

    monkeypatch.setattr(svc, "IncidentioClient", fake_client)
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: {"api_key": "key-1"})
    monkeypatch.setattr(svc, "FRONTEND_URL", "http://localhost:3000")
    # incidentio_alerts row the incident was created from: (incident_id, payload)
    patched_db.cursor.fetchone.return_value = ("inc_1", INCIDENT_EVENT)
    return SimpleNamespace(pool=patched_db, client=client, built=built)


# --- client ------------------------------------------------------------------

def test_post_incident_update_sends_idempotency_key(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        response = MagicMock()
        response.json.return_value = {"incident_update": {"id": "upd_1"}}
        return response

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.post_incident_update("inc_1", "hello", idempotency_key="aurora-rca-i1") == {"incident_update": {"id": "upd_1"}}
    assert calls == [("POST", "/incident_updates", {"json": {"incident_id": "inc_1", "message": "hello", "idempotency_key": "aurora-rca-i1"}})]


def test_list_incident_alerts_filters_by_alert(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return MagicMock()

    monkeypatch.setattr(client, "_request", fake_request)
    client.list_incident_alerts("alert_1")
    assert calls == [("GET", "/incident_alerts", {"params": {"alert_id": "alert_1", "page_size": 50}})]


def test_list_incident_alerts_by_incident(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return MagicMock()

    monkeypatch.setattr(client, "_request", fake_request)
    client.list_incident_alerts(incident_id="inc_9")
    assert calls == [("GET", "/incident_alerts", {"params": {"page_size": 50, "incident_id": "inc_9"}})]
    with pytest.raises(ValueError):
        client.list_incident_alerts()


def test_http_errors_carry_the_status_code(monkeypatch):
    import requests

    client = IncidentioClient("k")
    response = MagicMock(status_code=422)

    def fake_request(*args, **kwargs):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr("routes.incidentio.incidentio_client.requests.request", fake_request)
    with pytest.raises(IncidentioAPIError) as exc:
        client.get_incident("inc_1")
    assert exc.value.status_code == 422
    assert exc.value.code == IncidentioAPIError.API_ERROR


def test_timeout_has_no_status_code(monkeypatch):
    import requests

    def fake_request(*args, **kwargs):
        raise requests.exceptions.Timeout()

    monkeypatch.setattr("routes.incidentio.incidentio_client.requests.request", fake_request)
    with pytest.raises(IncidentioAPIError) as exc:
        IncidentioClient("k").get_incident("inc_1")
    assert exc.value.status_code is None
    assert exc.value.code == IncidentioAPIError.TIMEOUT


# --- markdown note -------------------------------------------------------------

def test_markdown_note_layout():
    from utils.notifications.rca_note import compose_note_markdown

    note = compose_note_markdown("Because X.", "i1", "Nobody noticed.", "https://aurora.example.com/")
    assert note == (
        "**Aurora RCA**\n\n**Root cause**\n\nBecause X.\n\n**Impact**\n\nNobody noticed.\n\n"
        "- [Open the full investigation](https://aurora.example.com/incidents/i1)\n\n"
        "_Generated automatically by Aurora. Verify before acting._"
    )


def test_markdown_note_without_impact_or_link():
    from utils.notifications.rca_note import compose_note_markdown

    note = compose_note_markdown("Because X.", "i1")
    assert "**Impact**" not in note
    assert "Open the full investigation" not in note
    assert note.endswith("_Generated automatically by Aurora. Verify before acting._")


def test_markdown_note_neutralises_asterisks_but_keeps_identifiers():
    from utils.notifications.rca_note import compose_note_markdown

    body = "handler.py keeps a global _connection_pool; p95*2 nodes and 3*4 workers hit MAX_POOL_BROKEN."
    note = compose_note_markdown(body, "i1")
    assert "p95*2" not in note and "3*4" not in note
    assert "p95\u22172 nodes and 3\u22174 workers" in note
    assert "_connection_pool" in note and "MAX_POOL_BROKEN" in note
    assert "\\" not in note  # incident.io shows backslash escapes literally


# --- eligibility ---------------------------------------------------------------

@pytest.mark.parametrize("over, reason", [
    ({"source_type": "pagerduty"}, "not an incident.io incident"),
    ({"incidentio_update_id": "upd_0"}, "already posted"),
    ({"incidentio_update_id": "pending"}, "already posted"),
    ({"source_alert_id": None}, "no source event"),
    ({"aurora_summary": "Too short."}, "summary too short"),
    ({"aurora_summary": None}, "summary too short"),
])
def test_eligibility_rejects(over, reason):
    ok, why, _, _ = svc._eligibility(_anchor(**over))
    assert ok is False
    assert reason in why


def test_eligibility_accepts_a_well_formed_anchor():
    assert svc._eligibility(_anchor()) == (True, "", ROOT_CAUSE_BODY, "")


# --- send_incidentio_incident_update -----------------------------------------

def test_incident_event_posts_to_that_incident(wired):
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.pool.updates == [CLAIM, RECORD]
    wired.client.list_incident_alerts.assert_not_called()
    wired.client.post_incident_update.assert_called_once()
    args, kwargs = wired.client.post_incident_update.call_args
    assert args[0] == "inc_1"
    assert kwargs == {"idempotency_key": "aurora-rca-i1"}
    content = args[1]
    assert content.startswith("**Aurora RCA**\n\n**Root cause**\n\n" + ROOT_CAUSE_BODY + "\n\n")
    assert "- [Open the full investigation](http://localhost:3000/incidents/i1)" in content
    assert content.endswith("_Generated automatically by Aurora. Verify before acting._")
    assert wired.built == ["key-1"]


def test_source_event_is_read_by_the_incident_source_alert_id(wired):
    svc.send_incidentio_incident_update("u1", _anchor(source_alert_id=42))
    sql, params = wired.pool.executes[0]
    assert sql == "SELECT incident_id, payload FROM incidentio_alerts WHERE id = %s"
    assert params == (42,)


def test_alert_event_posts_to_the_incident_it_is_attached_to(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT)
    wired.client.list_incident_alerts.return_value = _attached({"id": "inc_9", "status_category": "active"})
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    wired.client.list_incident_alerts.assert_called_once_with("alert_1")
    assert wired.client.post_incident_update.call_args.args[0] == "inc_9"
    assert wired.pool.updates == [CLAIM, RECORD]


def test_alert_event_stored_as_json_text_is_still_recognised(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", json.dumps(ALERT_EVENT))
    wired.client.list_incident_alerts.return_value = _attached({"id": "inc_9", "status_category": "triage"})
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    wired.client.list_incident_alerts.assert_called_once_with("alert_1")


def test_alert_skips_incidents_nobody_reads_any_more(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT)
    wired.client.list_incident_alerts.return_value = _attached(
        {"id": "inc_old", "status_category": "merged"},
        {"id": "inc_dup", "status_category": "declined"},
        {"id": "inc_live", "status_category": "active"},
    )
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_live"


# --- recurrences: a folded incident.io incident is still someone else's incident --------

def test_incident_event_recurrence_still_posts(wired):
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    wired.client.list_incident_alerts.assert_not_called()
    assert wired.pool.updates == [CLAIM, RECORD]


def _links_by(alert_filter, incident_filter):
    def side_effect(alert_id=None, incident_id=None):
        return _attached(*alert_filter) if alert_id else {"incident_alerts": incident_filter}
    return side_effect


def test_alert_recurrence_skips_when_anchor_alert_is_on_the_same_incident(wired):
    # source event of this recurrence, then the anchor's (alert id, update id)
    wired.pool.cursor.fetchone.side_effect = [("alert_2", ALERT_EVENT), ("alert_1", "upd_0")]
    wired.client.list_incident_alerts.side_effect = _links_by(
        [{"id": "inc_9", "status_category": "active"}],
        [{"alert": {"id": "alert_1"}}, {"alert": {"id": "alert_2"}}],
    )
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is False
    wired.client.list_incident_alerts.assert_any_call(incident_id="inc_9")
    wired.client.post_incident_update.assert_not_called()
    assert wired.pool.updates == []


def test_alert_recurrence_posts_when_anchor_is_on_another_incident(wired):
    wired.pool.cursor.fetchone.side_effect = [("alert_2", ALERT_EVENT), ("alert_1", "upd_0")]
    wired.client.list_incident_alerts.side_effect = _links_by(
        [{"id": "inc_9", "status_category": "active"}],
        [{"alert": {"id": "alert_2"}}, {"alert": {"id": "alert_7"}}],
    )
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_9"


def test_alert_recurrence_posts_when_anchor_never_posted(wired):
    wired.pool.cursor.fetchone.side_effect = [("alert_2", ALERT_EVENT), ("alert_1", None)]
    wired.client.list_incident_alerts.side_effect = _links_by([{"id": "inc_9", "status_category": "active"}], [])
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    # no need to ask incident.io which alerts are attached when the anchor posted nothing
    assert all(c.kwargs.get("incident_id") is None for c in wired.client.list_incident_alerts.call_args_list)


def test_alert_without_an_incident_posts_nothing_and_claims_nothing(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.post_incident_update.assert_not_called()


def test_alert_lookup_failure_posts_nothing_and_claims_nothing(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT)
    wired.client.list_incident_alerts.side_effect = IncidentioAPIError(IncidentioAPIError.API_ERROR, 500)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.post_incident_update.assert_not_called()


def test_missing_source_row_posts_nothing(wired):
    wired.pool.cursor.fetchone.return_value = None
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.post_incident_update.assert_not_called()


def test_lost_claim_posts_nothing(wired):
    wired.pool.cursor.rowcount = 0
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]
    wired.client.post_incident_update.assert_not_called()


@pytest.mark.parametrize("code, status", [
    (IncidentioAPIError.FORBIDDEN, 403),
    (IncidentioAPIError.INVALID_KEY, 401),
    (IncidentioAPIError.API_ERROR, 422),
])
def test_rejected_post_releases_the_claim(wired, code, status):
    wired.client.post_incident_update.side_effect = IncidentioAPIError(code, status)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM, RELEASE]


@pytest.mark.parametrize("code, status", [
    (IncidentioAPIError.TIMEOUT, None),
    (IncidentioAPIError.UNREACHABLE, None),
    (IncidentioAPIError.API_ERROR, 500),
    (IncidentioAPIError.API_ERROR, 502),
    (IncidentioAPIError.API_ERROR, 504),
])
def test_unknown_outcome_keeps_the_claim(wired, code, status):
    # incident.io may have stored the update: never risk a duplicate notification
    wired.client.post_incident_update.side_effect = IncidentioAPIError(code, status)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]


def test_unexpected_error_keeps_the_claim_and_does_not_raise(wired):
    wired.client.post_incident_update.side_effect = ValueError("boom")
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]


def test_unresolvable_org_never_posts(wired, monkeypatch):
    monkeypatch.setattr(svc, "set_rls_context", lambda *a, **k: None)
    monkeypatch.setattr(postback_claim, "set_rls_context", lambda *a, **k: None)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    wired.client.post_incident_update.assert_not_called()


def test_not_connected_posts_nothing(wired, monkeypatch):
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: None)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.executes == []
    wired.client.post_incident_update.assert_not_called()


def test_ineligible_incident_never_touches_db_or_incidentio(wired):
    assert svc.send_incidentio_incident_update("u1", _anchor(source_type="pagerduty")) is False
    assert wired.pool.executes == []
    wired.client.post_incident_update.assert_not_called()
