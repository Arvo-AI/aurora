"""Fast unit tests: no credentials, Docker, Redis, database or LLM."""

import pytest

from services.grafana_alert_preview import preview_grafana_alert


def test_firing_alert_extracts_details():
    payload = {
        "title": "Grafana grouped alert", "status": "firing",
        "commonLabels": {"severity": "high"},
        "alerts": [{
            "status": "firing", "fingerprint": "abc",
            "labels": {"alertname": "CheckoutErrors", "service": "checkout"},
            "annotations": {"summary": "Elevated 5xx", "description": "Many 500 responses"},
        }],
    }
    result = preview_grafana_alert(payload)
    assert result["count"] == 1
    item = result["previews"][0]
    assert (item["title"], item["service"], item["severity"], item["status"]) == (
        "CheckoutErrors", "checkout", "high", "firing"
    )
    assert item["actionable"] is True
    assert item["description"] == "Many 500 responses"
    assert "Elevated 5xx" in item["investigation_description"]
    assert "No incident" in result["note"]


def test_resolved_alert_is_not_actionable():
    result = preview_grafana_alert({"alerts": [{"status": "resolved", "labels": {"alertname": "DiskFull"}}]})
    item = result["previews"][0]
    assert item["status"] == "resolved"
    assert item["actionable"] is False
    assert "Resolved" in item["reason"]


def test_multi_alert_has_independent_fields_and_states():
    payload = {"title": "Group", "status": "firing", "commonLabels": {"severity": "medium"},
               "alerts": [
                   {"status": "firing", "labels": {"alertname": "CPU", "service": "api"}},
                   {"status": "resolved", "labels": {"alertname": "Memory", "service": "worker", "severity": "low"}},
               ]}
    previews = preview_grafana_alert(payload)["previews"]
    assert len(previews) == 2
    assert [p["title"] for p in previews] == ["CPU", "Memory"]
    assert [p["service"] for p in previews] == ["api", "worker"]
    assert [p["actionable"] for p in previews] == [True, False]
    assert previews[1]["severity"] == "low"


@pytest.mark.parametrize("payload", [
    None, [], "not JSON object", {}, {"alerts": []}, {"alerts": "string"},
    {"alerts": [42]}, {"alerts": [{"labels": "invalid"}]},
    {"alerts": [{"annotations": []}]},
    {"alerts": [{}], "commonLabels": []},
    {"alerts": [{}] * 21},
])
def test_malformed_payloads_rejected(payload):
    with pytest.raises(ValueError):
        preview_grafana_alert(payload)


def test_unknown_or_pending_status_needs_review():
    for status in ("pending", "unknown"):
        item = preview_grafana_alert({"alerts": [{"status": status}]})["previews"][0]
        assert not item["actionable"]
        assert "review" in item["reason"]


def test_log_evidence_investigation_description():
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "Checkout500", "service": "checkout"}}]}
    log = "2026-11-14 INFO accepting request\n2026-11-14 ERROR HTTP 500 checkout failed\nTraceback (most recent call last):\nValueError: invalid order"
    result = preview_grafana_alert(payload, error_log=log)
    assert result["log_evidence"]["matching_lines"] == 2
    assert result["log_evidence"]["highlights"][0]["line_number"] == 2
    assert "checkout failed" in result["previews"][0]["investigation_description"]
    assert result["previews"][0]["actionable"] is True


def test_log_without_error_keywords_is_not_claimed_to_be_clean():
    result = preview_grafana_alert({"alerts": [{"status": "firing"}]}, error_log="INFO started\nINFO processing")
    assert result["log_evidence"]["provided"]
    assert result["log_evidence"]["matching_lines"] == 0
    assert "No obvious error-like lines" in result["previews"][0]["investigation_description"]


def test_log_is_not_assumed_specific_to_any_one_alert():
    payload = {"alerts": [
        {"status": "firing", "labels": {"alertname": "API"}},
        {"status": "resolved", "labels": {"alertname": "Worker"}},
    ]}
    result = preview_grafana_alert(payload, error_log="ERROR checkout timeout")
    assert result["count"] == 2
    assert result["previews"][1]["actionable"] is False
    assert all("shared across alerts" in item["investigation_description"] for item in result["previews"])


def test_pasted_log_secrets_are_masked_in_highlights_and_description():
    payload = {"alerts": [{"status": "firing"}]}
    log = "ERROR authorization=super-secret Bearer abc123 password=hunter2"
    result = preview_grafana_alert(payload, error_log=log)
    text = result["previews"][0]["investigation_description"]
    assert "super-secret" not in text and "abc123" not in text and "hunter2" not in text
    assert text.count("[REDACTED]") == 3


@pytest.mark.parametrize("log", [None, ["ERROR"], {"message": "error"}, 1, "x" * 32001, "ERROR\n" * 2001])
def test_invalid_or_oversized_logs_are_rejected(log):
    with pytest.raises(ValueError):
        preview_grafana_alert({"alerts": [{"status": "firing"}]}, error_log=log)


def test_old_api_still_works_without_log():
    result = preview_grafana_alert({"alerts": [{"status": "firing"}]})
    assert result["log_evidence"]["provided"] is False
    assert "User-pasted log evidence" not in result["previews"][0]["investigation_description"]
