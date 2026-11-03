"""get_incidentio_alert resolves an incident.io alert id or a source dedup key."""
import json
from unittest.mock import patch

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
    seen, patches = _calls(responses)
    with patches[0], patches[1]:
        payload = json.loads(tool.get_incidentio_alert(alert_id, user_id=user_id))
    return payload, seen


def test_requires_a_user_and_a_connection():
    assert json.loads(tool.get_incidentio_alert("01ABC"))["error"] == "No user context available"
    with patch.object(tool, "_get_incidentio_credentials", return_value=None):
        missing = json.loads(tool.get_incidentio_alert("01ABC", user_id="u1"))
    assert missing["error"] == "incident.io not connected"


def test_rejects_blank_oversized_and_control_ids():
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"):
        blank = json.loads(tool.get_incidentio_alert("  ", user_id="u1"))
        huge = json.loads(tool.get_incidentio_alert("a" * 101, user_id="u1"))
        broken = json.loads(tool.get_incidentio_alert("id\n1", user_id="u1"))
    assert blank["error"] == huge["error"] == broken["error"] == "Invalid alert_id"


def test_incidentio_alert_id_includes_the_attached_incident():
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


def test_source_alert_id_falls_back_to_the_deduplication_key():
    """A Site24x7-style id is not an incident.io alert id; it is the dedup key."""
    payload, seen = _run("4293868629", [{"error": "Resource not found"}, {"alerts": [ALERT]}, LINKS])

    assert payload["id"] == ALERT["id"]
    assert payload["incidents"][0]["id"] == "01INC"
    assert seen[0] == ("/alerts/4293868629", None)
    assert seen[1] == (
        "/alerts",
        {"page_size": 10, "deduplication_key[is]": "4293868629"},
    )
    assert seen[2][0] == "/incident_alerts"


def test_auth_failure_does_not_search_by_dedup_key():
    payload, seen = _run(
        "01ABC",
        [{"error": "Authentication failed — API key may be invalid or expired"}],
    )
    assert "Authentication" in payload["error"]
    assert seen == [("/alerts/01ABC", None)]


def test_source_key_with_a_slash_skips_the_path_lookup():
    payload, seen = _run("monitor/4293868629", [{"alerts": [ALERT]}, LINKS])

    assert payload["title"] == "CPU high"
    assert seen[0] == (
        "/alerts",
        {"page_size": 10, "deduplication_key[is]": "monitor/4293868629"},
    )


def test_shared_dedup_key_returns_every_match():
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
    page = [{**ALERT, "id": f"01ALERT{i:02d}"} for i in range(10)]
    responses = [{"alerts": page, "pagination_meta": {"after": "cursor"}}] + [LINKS] * 10
    payload, _ = _run("monitor/4293868629", responses)
    assert payload["has_more"] is True
    assert payload["total_returned"] == 10


def test_missing_incident_points_at_the_alert_tool():
    with patch.object(tool, "_get_incidentio_credentials", return_value="key"), \
            patch.object(tool, "_api_request", return_value={"error": "Resource not found"}):
        payload = json.loads(tool.get_incidentio_incident("4293868629", user_id="u1"))
    assert "get_incidentio_alert" in payload["error"]


def test_unknown_id_and_a_failed_attachment_lookup():
    missing, _ = _run("4293868629", [{"error": "Resource not found"}, {"alerts": []}])
    assert missing["error"] == "No incident.io alert found for that id"

    payload, _ = _run(
        ALERT["id"],
        [{"alert": ALERT}, {"error": "incident.io API error (HTTP 500)"}],
    )
    assert payload["id"] == ALERT["id"]
    assert payload["incidents_error"] == "incident.io API error (HTTP 500)"
    assert "incidents" not in payload
