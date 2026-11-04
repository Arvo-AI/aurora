"""get_incidentio_alert resolves an incident.io alert id or a source dedup key."""
import json
from unittest.mock import patch

import requests

import chat.backend.agent.tools.incidentio_tool as tool

ALERT = {
    "id": "01GW2G3V0S59R238FAHPDS1R66",
    "title": "CPU high",
    "description": "CPU exceeded 75%",
    "status": "firing",
    "deduplication_key": "4293868629",
    "source_url": "https://example.com/alerts/4293868629",
    "alert_source_id": "01SRC",
    "created_at": "2021-08-17T13:28:57Z",
    "attributes": [
        {"attribute": {"name": "Service"}, "value": {"label": "payments"}},
        {
            "attribute": {"name": "Team"},
            "value": [{"label": "oncall"}, {"label": "payments"}],
        },
    ],
    "alert_group_ids": ["01GRP"],
}

LINKS = {
    "incident_alerts": [
        {
            "incident": {
                "id": "01INC",
                "name": "Payments down",
                "reference": "INC-12",
                "status_category": "live",
            }
        }
    ]
}


def _calls(responses):
    """Patch credentials and the API, recording (path, params) in order."""
    seen = []

    def fake(api_key, method, path, params=None):
        seen.append((path, params))
        response = responses[len(seen) - 1]
        return response() if callable(response) else response

    return seen, (
        patch.object(tool, "_get_incidentio_credentials", return_value="key"),
        patch.object(tool, "_api_request", side_effect=fake),
    )


def _run(alert_id, responses, user_id="u1"):
    """Call the tool with a scripted API and return (payload, calls)."""
    seen, patches = _calls(responses)
    with patches[0], patches[1]:
        payload = json.loads(tool.get_incidentio_alert(alert_id, user_id=user_id))
    return payload, seen


def test_requires_a_user_and_a_connection():
    """Missing user context and a missing connection are distinct errors."""
    assert json.loads(tool.get_incidentio_alert("01ABC"))["error"] == "No user context available"
    with patch.object(tool, "_get_incidentio_credentials", return_value=None):
        missing = json.loads(tool.get_incidentio_alert("01ABC", user_id="u1"))
    assert missing["error"] == "incident.io not connected"


def test_rejects_blank_oversized_and_control_ids():
    """Blank, oversized, and control-character ids never reach the API."""
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"):
        blank = json.loads(tool.get_incidentio_alert("  ", user_id="u1"))
        huge = json.loads(tool.get_incidentio_alert("a" * 101, user_id="u1"))
        broken = json.loads(tool.get_incidentio_alert("id\n1", user_id="u1"))
    assert blank["error"] == huge["error"] == broken["error"] == "Invalid alert_id"


def test_incidentio_alert_id_includes_the_attached_incident():
    """A ULID is fetched by path and comes back with the incident it is on."""
    payload, seen = _run(ALERT["id"], [{"alert": ALERT}, LINKS])

    assert payload["id"] == ALERT["id"]
    assert payload["deduplication_key"] == "4293868629"
    assert payload["attributes"] == [
        {"name": "Service", "value": "payments"},
        {"name": "Team", "value": "oncall, payments"},
    ]
    assert payload["incidents"] == [
        {
            "id": "01INC",
            "name": "Payments down",
            "reference": "INC-12",
            "status": None,
            "status_category": "live",
        }
    ]
    assert seen == [
        (f"/alerts/{ALERT['id']}", None),
        ("/incident_alerts", {"alert_id": ALERT["id"], "page_size": 25}),
    ]


def test_source_alert_id_is_looked_up_as_a_deduplication_key():
    """A Site24x7-style id is not an incident.io ULID, so it never hits the path."""
    payload, seen = _run("4293868629", [{"alerts": [ALERT]}, LINKS])

    assert payload["id"] == ALERT["id"]
    assert payload["incidents"][0]["id"] == "01INC"
    assert seen[0] == (
        "/alerts",
        {"page_size": 10, "deduplication_key[is]": "4293868629"},
    )
    assert seen[1][0] == "/incident_alerts"


def test_rejected_ulid_still_searches_by_dedup_key():
    """HTTP 400 on the path is not fatal; the same id may be a dedup key."""
    payload, seen = _run(
        ALERT["id"],
        [{"error": "incident.io API error (HTTP 400)"}, {"alerts": [ALERT]}, LINKS],
    )
    assert payload["deduplication_key"] == "4293868629"
    assert seen[0] == (f"/alerts/{ALERT['id']}", None)
    assert seen[1][0] == "/alerts"


def test_auth_failure_does_not_search_by_dedup_key():
    """An auth failure on the path lookup is final."""
    payload, seen = _run(
        ALERT["id"],
        [{"error": "Authentication failed — API key may be invalid or expired"}],
    )
    assert "Authentication" in payload["error"]
    assert seen == [(f"/alerts/{ALERT['id']}", None)]


def test_source_key_with_a_slash_skips_the_path_lookup():
    """A key that is not a ULID is only searched as a deduplication key."""
    payload, seen = _run("monitor/4293868629", [{"alerts": [ALERT]}, LINKS])

    assert payload["title"] == "CPU high"
    assert seen[0] == (
        "/alerts",
        {"page_size": 10, "deduplication_key[is]": "monitor/4293868629"},
    )


def test_shared_dedup_key_returns_every_match():
    """One source key on two alert sources returns both, with attachments."""
    other = {**ALERT, "id": "01OTHER", "title": "Other source"}
    payload, seen = _run(
        "monitor/4293868629",
        [{"alerts": [ALERT, other], "pagination_meta": {"after": "cursor"}}, LINKS, LINKS],
    )

    assert payload["matched_on"] == "deduplication_key"
    # A cursor on a short page is the last page, not another page of matches.
    assert payload["has_more"] is False
    assert [item["id"] for item in payload["matches"]] == [ALERT["id"], "01OTHER"]
    assert payload["matches"][0]["incidents"][0]["id"] == "01INC"
    assert [path for path, _ in seen] == ["/alerts", "/incident_alerts", "/incident_alerts"]


def test_full_page_of_matches_reports_more():
    """A full page plus a cursor means another page may exist."""
    page = [{**ALERT, "id": f"01ALERT{i:02d}"} for i in range(10)]
    responses = [{"alerts": page, "pagination_meta": {"after": "cursor"}}] + [LINKS] * 10
    payload, _ = _run("monitor/4293868629", responses)
    assert payload["has_more"] is True
    assert payload["total_returned"] == 10


def _http_response(status, body=None):
    """A real requests.Response. Error statuses are false, which is the bug."""
    resp = requests.Response()
    resp.status_code = status
    resp.url = f"{tool.INCIDENTIO_API_BASE}/alerts/x"
    resp.reason = "OK" if status < 400 else "Error"
    if body is not None:
        resp._content = json.dumps(body).encode()
        resp.headers["Content-Type"] = "application/json"
    return resp


def test_error_status_is_read_from_a_real_response():
    """A 4xx Response is false, so the status has to be read with an explicit check."""
    with patch("requests.request", return_value=_http_response(404)):
        missing = tool._api_request("key", "GET", "/alerts/01ABC")
    assert missing == {"error": "Resource not found"}

    with patch("requests.request", return_value=_http_response(400)):
        rejected = tool._api_request("key", "GET", "/alerts/01ABC")
    assert rejected["error"] in tool._PATH_MISS

    with patch("requests.request", return_value=_http_response(401)):
        denied = tool._api_request("key", "GET", "/alerts/01ABC")
    assert denied["error"].startswith("Authentication failed")


def test_real_404_on_a_ulid_still_searches_by_dedup_key():
    """The path miss has to be the string _api_request actually returns."""
    responses = [
        _http_response(404),
        _http_response(200, {"alerts": [ALERT]}),
        _http_response(200, LINKS),
    ]

    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch("requests.request", side_effect=responses):
        payload = json.loads(tool.get_incidentio_alert(ALERT["id"], user_id="u1"))
    assert payload["id"] == ALERT["id"]
    assert payload["incidents"][0]["id"] == "01INC"


def test_real_incident_404_points_at_the_alert_tool():
    """The incident hint only runs when a 404 stays 'Resource not found'."""
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch("requests.request", return_value=_http_response(404)):
        payload = json.loads(tool.get_incidentio_incident("4293868629", user_id="u1"))
    assert payload["error"].startswith("Resource not found")
    assert "get_incidentio_alert" in payload["error"]


def test_missing_incident_points_at_the_alert_tool():
    """A 404 on an incident id tells the agent to try the alert tool."""
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch.object(tool, "_api_request", return_value={"error": "Resource not found"}):
        payload = json.loads(tool.get_incidentio_incident("4293868629", user_id="u1"))
    assert "get_incidentio_alert" in payload["error"]


def test_unknown_id_and_a_failed_attachment_lookup():
    """An unknown key is an error; an attachment failure still returns the alert."""
    missing, _ = _run("4293868629", [{"alerts": []}])
    assert missing["error"] == "No incident.io alert found for that id"

    payload, _ = _run(
        ALERT["id"],
        [{"alert": ALERT}, {"error": "incident.io API error (HTTP 500)"}],
    )
    assert payload["id"] == ALERT["id"]
    assert payload["incidents_error"] == "incident.io API error (HTTP 500)"
    assert "incidents" not in payload


def test_oversized_matches_stay_valid_json():
    """A matches payload over the cap drops records instead of slicing the JSON."""
    payload = {
        "matches": [{"id": str(i), "description": "x" * 200} for i in range(20)],
        "total_returned": 20,
        "matched_on": "deduplication_key",
    }
    with patch.object(tool, "MAX_OUTPUT_SIZE", 500):
        raw = tool._truncate_output(payload)
    parsed = json.loads(raw)
    assert parsed["truncated"] is True
    assert parsed["total_returned"] == len(parsed["matches"])
    assert len(parsed["matches"]) < 20


def test_list_alerts_returns_ids_and_follows_the_cursor():
    """A cursor on this endpoint means another page, even when the page is short."""
    seen = []

    def fake(api_key, method, path, params=None):
        seen.append((path, params))
        return {
            "alerts": [ALERT, "not-a-dict"],
            "pagination_meta": {"after": "cursor"},
        }

    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch.object(tool, "_api_request", side_effect=fake):
        payload = json.loads(tool.list_incidentio_alerts(" FIRING ", page_size=80, user_id="u1"))

    assert payload["alerts"] == [{
        "id": ALERT["id"],
        "title": "CPU high",
        "status": "firing",
        "deduplication_key": "4293868629",
        "source_url": ALERT["source_url"],
        "alert_source_id": "01SRC",
        "created_at": ALERT["created_at"],
        "resolved_at": None,
    }]
    assert payload["has_more"] is True
    assert payload["next_cursor"] == "cursor"
    assert seen == [("/alerts", {"page_size": 50, "status[one_of]": "firing"})]


def test_list_alerts_rejects_a_bad_status_and_a_missing_connection():
    """Status is firing or resolved, and a missing connection never reaches the API."""
    assert json.loads(tool.list_incidentio_alerts())["error"] == "No user context available"
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"):
        bad = json.loads(tool.list_incidentio_alerts(status="live", user_id="u1"))
    assert "firing" in bad["error"]

    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch.object(tool, "_api_request", return_value={"alerts": [ALERT]}):
        done = json.loads(tool.list_incidentio_alerts(user_id="u1"))
    assert done["has_more"] is False
    assert "next_cursor" not in done
