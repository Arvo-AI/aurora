from chat.backend.agent.tools.splunk_on_call_tool import _filter_incidents
from routes.splunk_on_call.tasks import _incidents, _normalize


def test_normalize_public_api_incident():
    incident = _normalize({
        "incidentNumber": "3453140",
        "currentPhase": "UNACKED",
        "entityDisplayName": "vector lag for engvis",
        "entityState": "CRITICAL",
        "routingKey": "team-engvis-primary",
        "service": "vector",
    })

    assert incident["incident_number"] == "3453140"
    assert incident["phase"] == "UNACKED"
    assert incident["title"] == "vector lag for engvis"
    assert incident["routing_key"] == "team-engvis-primary"
    assert incident["entity_state"] == "CRITICAL"


def test_normalize_outbound_webhook_snake_case():
    incident = _normalize({
        "incident_number": "42",
        "state": "resolved",
        "entity_display_name": "service recovered",
        "routing_key": "engvis",
        "monitoring_tool": "mimir",
    })

    assert incident["incident_number"] == "42"
    assert incident["phase"] == "RESOLVED"
    assert incident["service"] == "mimir"


def test_normalize_default_nested_outbound_payload():
    incident = _normalize({
        "INCIDENT": {
            "INCIDENT_ID": "77",
            "CURRENT_PHASE": "ACKED",
            "SERVICE": "vector",
            "ENTITY_STATE": "CRITICAL",
        },
        "STATE": {"INCIDENT_NAME": "vector lag", "HOST": "node-1"},
        "ALERT": {"routing_key": "engvis-primary", "entity_id": "vector/node-1"},
    })

    assert incident["incident_number"] == "77"
    assert incident["phase"] == "ACKED"
    assert incident["title"] == "vector lag"
    assert incident["routing_key"] == "engvis-primary"
    assert incident["host"] == "node-1"


def test_incidents_accepts_list_and_single_payloads():
    listed = _incidents({
        "incidents": [{"incidentNumber": "1"}, {"incidentNumber": "2"}],
    })
    single = _incidents({"incidentNumber": "3"})

    assert [item["incidentNumber"] for item in listed] == ["1", "2"]
    assert single[0]["incidentNumber"] == "3"


def test_filter_incidents_by_phase_and_routing_key():
    incidents = [
        {"incidentNumber": "1", "currentPhase": "UNACKED", "routingKey": "team-alpha"},
        {"incidentNumber": "2", "currentPhase": "ACKED", "routingKey": "team-alpha"},
        {"incidentNumber": "3", "currentPhase": "UNACKED", "routingKey": "team-beta"},
    ]

    results, truncated = _filter_incidents(incidents, "unacked", "alpha", 50)

    assert [item["incidentNumber"] for item in results] == ["1"]
    assert truncated is False
