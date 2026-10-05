"""incidentio_notification_service: post the RCA back as an incident.io incident update or alert note (no DB, no network)."""

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
# Notes claim a tagged token so an in-flight note is not read as an incident update
NOTE_CLAIM = ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s AND incidentio_update_id IS NULL", ("note:pending", "i1"))
NOTE_RELEASE = ("UPDATE incidents SET incidentio_update_id = NULL WHERE id = %s AND incidentio_update_id = %s", ("i1", "note:pending"))
RECORD = ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s", ("upd_1", "i1"))
RECORD_NOTE = ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s", ("note:note_1", "i1"))


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
    client.post_alert_note.return_value = {"alert_note": {"id": "note_1"}}
    client.list_incident_alerts.return_value = _attached()
    built = []

    def fake_client(api_key):
        built.append(api_key)
        return client

    monkeypatch.setattr(svc, "IncidentioClient", fake_client)
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: {"api_key": "key-1"})
    monkeypatch.setattr(svc, "FRONTEND_URL", "http://localhost:3000")
    # source row: (incident.io object id, payload, anchor object id, anchor update id)
    patched_db.cursor.fetchone.return_value = ("inc_1", INCIDENT_EVENT, None, None)
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


def test_post_alert_note_targets_the_v1_service(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        response = MagicMock()
        response.json.return_value = {"alert_note": {"id": "note_1"}}
        return response

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.post_alert_note("alert_1", "hello") == {"alert_note": {"id": "note_1"}}
    # alert notes are v1; no idempotency_key exists on this endpoint
    assert calls == [("POST", "/alert_notes", {"api_version": "v1", "json": {"alert_id": "alert_1", "content": "hello"}})]


def _captured_urls(monkeypatch):
    """Record the absolute URL every _request builds."""
    urls = []

    def fake_request(method, url, **kwargs):
        urls.append(url)
        response = MagicMock(status_code=200)
        response.json.return_value = {}
        return response

    monkeypatch.setattr("routes.incidentio.incidentio_client.requests.request", fake_request)
    return urls


def test_alert_notes_use_v1_while_every_other_call_stays_on_v2(monkeypatch):
    urls = _captured_urls(monkeypatch)
    client = IncidentioClient("k")
    client.get_incident("inc_1")
    client.post_incident_update("inc_1", "hello")
    client.post_alert_note("alert_1", "hello")
    assert urls == [
        "https://api.incident.io/v2/incidents/inc_1",
        "https://api.incident.io/v2/incident_updates",
        "https://api.incident.io/v1/alert_notes",
    ]


def _page(links, after=None):
    response = MagicMock()
    response.json.return_value = {"incident_alerts": links, "pagination_meta": {"after": after, "page_size": 50}}
    return response


def test_list_incident_alerts_filters_by_alert(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _page([{"id": "link_1"}], after="cursor-1")  # short page: no second request

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.list_incident_alerts("alert_1") == {"incident_alerts": [{"id": "link_1"}]}
    assert calls == [("GET", "/incident_alerts", {"params": {"alert_id": "alert_1", "page_size": 50}})]


def test_list_incident_alerts_pages_through_a_full_incident(monkeypatch):
    # an incident with >50 attached alerts: the anchor's alert may sit on page 2
    client = IncidentioClient("k")
    pages = [
        _page([{"alert": {"id": f"alert_{i}"}} for i in range(50)], after="c1"),
        _page([{"alert": {"id": "alert_50"}}, {"alert": {"id": "anchor"}}], after="c2"),
    ]
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(kwargs["params"])
        return pages.pop(0)

    monkeypatch.setattr(client, "_request", fake_request)
    links = client.list_incident_alerts(incident_id="inc_9")["incident_alerts"]
    assert len(links) == 52
    assert links[-1]["alert"]["id"] == "anchor"
    assert calls == [
        {"page_size": 50, "incident_id": "inc_9"},
        {"page_size": 50, "incident_id": "inc_9", "after": "c1"},
    ]


def test_list_incident_alerts_by_incident(monkeypatch):
    client = IncidentioClient("k")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _page([])

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
    client = IncidentioClient("k")
    with pytest.raises(IncidentioAPIError) as exc:
        client.get_incident("inc_1")
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
    assert "p95*2" not in note
    assert "3*4" not in note
    assert "p95\u22172 nodes and 3\u22174 workers" in note
    assert "_connection_pool" in note
    assert "MAX_POOL_BROKEN" in note
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
    wired.client.post_alert_note.assert_not_called()
    wired.client.post_incident_update.assert_called_once()
    args, kwargs = wired.client.post_incident_update.call_args
    assert args[0] == "inc_1"
    assert kwargs == {"idempotency_key": "aurora-rca-i1"}
    content = args[1]
    assert content.startswith("**Aurora RCA**\n\n**Root cause**\n\n" + ROOT_CAUSE_BODY + "\n\n")
    assert "- [Open the full investigation](http://localhost:3000/incidents/i1)" in content
    assert content.endswith("_Generated automatically by Aurora. Verify before acting._")
    assert wired.built == ["key-1"]


def test_source_and_anchor_are_read_in_one_query(wired):
    svc.send_incidentio_incident_update("u1", _anchor(source_alert_id=42, recurrence_of="root-1"))
    reads = [(sql, params) for sql, params in wired.pool.executes if sql.startswith("SELECT")]
    assert len(reads) == 1
    sql, params = reads[0]
    assert sql.startswith("SELECT a.incident_id, a.payload, anc_a.incident_id, anc.incidentio_update_id FROM incidentio_alerts a")
    assert "LEFT JOIN incidents anc ON anc.id = %s" in sql
    assert params == ("root-1", 42)


def test_alert_on_several_open_incidents_picks_the_live_one_then_the_oldest(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.list_incident_alerts.return_value = _attached(
        {"id": "inc_closed", "status_category": "closed", "external_id": 1},
        {"id": "inc_live_new", "status_category": "live", "external_id": 40},
        {"id": "inc_live_old", "status_category": "live", "external_id": 12},
        {"id": "inc_triage", "status_category": "triage", "external_id": 3},
    )
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_live_old"


def test_alert_event_posts_to_the_incident_it_is_attached_to(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.list_incident_alerts.return_value = _attached({"id": "inc_9", "status_category": "live"})
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    wired.client.list_incident_alerts.assert_called_once_with("alert_1")
    assert wired.client.post_incident_update.call_args.args[0] == "inc_9"
    # an attached alert has an incident timeline: no alert-note fallback
    wired.client.post_alert_note.assert_not_called()
    assert wired.pool.updates == [CLAIM, RECORD]


def test_alert_event_stored_as_json_text_is_still_recognised(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", json.dumps(ALERT_EVENT), None, None)
    wired.client.list_incident_alerts.return_value = _attached({"id": "inc_9", "status_category": "triage"})
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    wired.client.list_incident_alerts.assert_called_once_with("alert_1")


def test_alert_skips_incidents_nobody_reads_any_more(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.list_incident_alerts.return_value = _attached(
        {"id": "inc_old", "status_category": "merged"},
        {"id": "inc_dup", "status_category": "declined"},
        {"id": "inc_live", "status_category": "live"},
    )
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_live"


def test_alert_attached_only_to_a_closed_incident_still_posts_there(wired):
    # closed still has a timeline (the channel, the postmortem); the alert-note
    # fallback is for an alert with no incident left to post on
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.list_incident_alerts.return_value = _attached(
        {"id": "inc_done", "status_category": "closed"},
    )
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_done"
    wired.client.post_alert_note.assert_not_called()


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
    # this recurrence's alert, and its anchor's alert which already posted
    wired.pool.cursor.fetchone.return_value = ("alert_2", ALERT_EVENT, "alert_1", "upd_0")
    wired.client.list_incident_alerts.side_effect = _links_by(
        [{"id": "inc_9", "status_category": "live"}],
        [{"alert": {"id": "alert_1"}}, {"alert": {"id": "alert_2"}}],
    )
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is False
    wired.client.list_incident_alerts.assert_any_call(incident_id="inc_9")
    wired.client.post_incident_update.assert_not_called()
    # folded onto the anchor's incident: it must not fall through to an alert note either
    wired.client.post_alert_note.assert_not_called()
    assert wired.pool.updates == []


def test_alert_recurrence_posts_when_anchor_is_on_another_incident(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_2", ALERT_EVENT, "alert_1", "upd_0")
    wired.client.list_incident_alerts.side_effect = _links_by(
        [{"id": "inc_9", "status_category": "live"}],
        [{"alert": {"id": "alert_2"}}, {"alert": {"id": "alert_7"}}],
    )
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_9"


def test_alert_recurrence_posts_when_anchor_never_posted(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_2", ALERT_EVENT, "alert_1", None)
    wired.client.list_incident_alerts.side_effect = _links_by([{"id": "inc_9", "status_category": "live"}], [])
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    # no need to ask incident.io which alerts are attached when the anchor posted nothing
    assert all(c.kwargs.get("incident_id") is None for c in wired.client.list_incident_alerts.call_args_list)


def test_alert_without_an_incident_posts_a_note_on_the_alert(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    wired.client.post_incident_update.assert_not_called()
    wired.client.post_alert_note.assert_called_once()
    alert_id, content = wired.client.post_alert_note.call_args.args
    assert alert_id == "alert_1"
    assert content.startswith("**Aurora RCA**\n\n**Root cause**\n\n" + ROOT_CAUSE_BODY + "\n\n")
    assert "- [Open the full investigation](http://localhost:3000/incidents/i1)" in content
    # the note id shares the claim column with incident updates, tagged so the two are distinguishable
    assert wired.pool.updates == [NOTE_CLAIM, RECORD_NOTE]


def test_alert_note_without_a_returned_id_still_resolves_the_claim(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.post_alert_note.return_value = {}
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert wired.pool.updates == [NOTE_CLAIM, ("UPDATE incidents SET incidentio_update_id = %s WHERE id = %s", ("note:posted", "i1"))]


def test_alert_note_is_claimed_before_the_post(wired):
    # /v1/alert_notes has no idempotency key, so the claim is the only duplicate guard
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    order = []

    def record_claim(sql, params=None):
        if "incidentio_update_id IS NULL" in sql:
            order.append("claim")

    def record_post(alert_id, content):
        order.append("post")
        return {"alert_note": {"id": "note_1"}}

    wired.pool.cursor.execute.side_effect = record_claim
    wired.client.post_alert_note.side_effect = record_post
    assert svc.send_incidentio_incident_update("u1", _anchor()) is True
    assert order == ["claim", "post"]


def test_lost_claim_posts_no_alert_note(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.pool.cursor.rowcount = 0
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [NOTE_CLAIM]
    wired.client.post_alert_note.assert_not_called()


def test_incident_sourced_rca_with_no_incident_id_still_posts_nothing(wired):
    # an incident event whose payload carries no incident id has nowhere to post:
    # there is no alert to fall back onto
    wired.pool.cursor.fetchone.return_value = (None, {"event_type": "public_incident.incident_created_v2", "event": {}}, None, None)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.post_incident_update.assert_not_called()
    wired.client.post_alert_note.assert_not_called()


def test_missing_alerts_edit_scope_releases_the_claim_and_does_not_raise(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.post_alert_note.side_effect = IncidentioAPIError(IncidentioAPIError.FORBIDDEN, 403)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [NOTE_CLAIM, NOTE_RELEASE]


@pytest.mark.parametrize("code, status", [
    (IncidentioAPIError.INVALID_KEY, 401),
    (IncidentioAPIError.API_ERROR, 422),
])
def test_rejected_alert_note_releases_the_claim(wired, code, status):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.post_alert_note.side_effect = IncidentioAPIError(code, status)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [NOTE_CLAIM, NOTE_RELEASE]


@pytest.mark.parametrize("code, status", [
    (IncidentioAPIError.TIMEOUT, None),
    (IncidentioAPIError.UNREACHABLE, None),
    (IncidentioAPIError.API_ERROR, 500),
    (IncidentioAPIError.API_ERROR, 502),
    (IncidentioAPIError.API_ERROR, 504),
])
def test_unknown_alert_note_outcome_keeps_the_claim(wired, code, status):
    # with no idempotency key on /v1/alert_notes, a retry would duplicate the note
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.post_alert_note.side_effect = IncidentioAPIError(code, status)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == [NOTE_CLAIM]


def test_alert_note_recurrence_posts_its_own_note(wired):
    # each alert of a recurrence group is its own Aurora incident with its own claim and
    # its own alert timeline, so an escalation-only recurrence is not folded away
    wired.pool.cursor.fetchone.return_value = ("alert_2", ALERT_EVENT, "alert_1", "note:note_0")
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    assert wired.client.post_alert_note.call_args.args[0] == "alert_2"
    assert wired.pool.updates == [NOTE_CLAIM, RECORD_NOTE]


@pytest.mark.parametrize("anchor_claim", ["note:note_0", "note:pending"])
def test_an_anchor_note_is_not_mistaken_for_incident_coverage(wired, anchor_claim):
    # the anchor only noted its own alert (or is still posting that note); this
    # recurrence's alert did get attached to an incident, whose timeline nothing
    # has posted to yet -> post the update there
    wired.pool.cursor.fetchone.return_value = ("alert_2", ALERT_EVENT, "alert_1", anchor_claim)
    wired.client.list_incident_alerts.side_effect = _links_by(
        [{"id": "inc_9", "status_category": "live"}],
        [{"alert": {"id": "alert_1"}}, {"alert": {"id": "alert_2"}}],
    )
    assert svc.send_incidentio_incident_update("u1", _anchor(recurrence_of="root-1")) is True
    assert wired.client.post_incident_update.call_args.args[0] == "inc_9"
    # no point asking which alerts are attached: a note can never cover an incident
    assert all(c.kwargs.get("incident_id") is None for c in wired.client.list_incident_alerts.call_args_list)


def test_alert_lookup_failure_posts_nothing_and_claims_nothing(wired):
    wired.pool.cursor.fetchone.return_value = ("alert_1", ALERT_EVENT, None, None)
    wired.client.list_incident_alerts.side_effect = IncidentioAPIError(IncidentioAPIError.API_ERROR, 500)
    assert svc.send_incidentio_incident_update("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.post_incident_update.assert_not_called()
    wired.client.post_alert_note.assert_not_called()


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
