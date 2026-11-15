"""Pure prompt-construction tests; no LLM, Redis, Celery or credentials."""
import pytest

from services.grafana_alert_preview import preview_grafana_alert
from services.grafana_preview_analysis import build_preview_analysis_prompt


def _preview(status="firing", log="ERROR HTTP 500 /checkout\nINFO backoff"):
    return preview_grafana_alert({"alerts": [{"status": status, "labels": {
        "alertname": "CheckoutErrors", "service": "checkout"}}]}, error_log=log)


def test_firing_alert_prompt_is_bounded_and_read_only():
    prompt, rail = build_preview_analysis_prompt(_preview())
    assert "ERROR HTTP 500" in prompt
    assert "Log line 1" in prompt
    assert "only evidence" in prompt
    assert "read-only" in prompt
    assert "untrusted_evidence" in prompt
    assert rail in prompt
    assert len(prompt) < 8000


def test_resolved_alert_rejected():
    with pytest.raises(ValueError, match="Only firing"):
        build_preview_analysis_prompt(_preview("resolved"))


@pytest.mark.parametrize("idx", [-1, 1, 1.2, "0", True])
def test_invalid_alert_index_rejected(idx):
    with pytest.raises(ValueError, match="alert_index"):
        build_preview_analysis_prompt(_preview(), idx)


def test_selected_alert_is_used():
    p = preview_grafana_alert({"alerts": [
        {"status": "firing", "labels": {"alertname": "API"}},
        {"status": "firing", "labels": {"alertname": "Worker"}},
    ]})
    prompt, _ = build_preview_analysis_prompt(p, 1)
    assert "Worker" in prompt
    assert "Investigate alert 'API'" not in prompt


def test_pasted_secret_patterns_redacted_in_prompt():
    prompt, rail = build_preview_analysis_prompt(_preview(log="ERROR password=hunter2 Bearer abc123"))
    assert "hunter2" not in prompt and "abc123" not in prompt
    assert "[REDACTED]" in rail


def test_does_not_include_full_log():
    prompt, _ = build_preview_analysis_prompt(_preview(log="INFO confidential debug only\nERROR retry failed"))
    assert "confidential debug only" not in prompt
    assert "retry failed" in prompt


def test_prompt_overflow_rejected():
    p = _preview()
    p["previews"][0]["investigation_description"] = "x" * 6501
    with pytest.raises(ValueError, match="too long"):
        build_preview_analysis_prompt(p)
