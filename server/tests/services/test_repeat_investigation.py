"""A repeat of an alert that already has an incident must not start another RCA.

Metric samples and templated annotation text change on every evaluation.
Those are not a new problem. A resolved incident firing again, a real change
in title, service, severity, or labels, or an enqueue that never landed, is.
"""

from datetime import datetime, timedelta, timezone

from services.incidents.repeat_investigation import (
    ExistingIncident,
    RCA_ENQUEUE_CLAIM,
    alert_signature,
    metadata_labels,
    repeat_needs_investigation,
    should_start_investigation,
)


def _sig(**overrides):
    base = dict(
        title="HighCPU",
        service="api",
        severity="critical",
        labels={"alertname": "HighCPU", "service": "api"},
    )
    base.update(overrides)
    return alert_signature(base["title"], base["service"], base["severity"], base["labels"])


def _existing(**overrides):
    fields = dict(
        status="investigating",
        title="HighCPU",
        service="api",
        severity="critical",
        metadata={"labels": {"alertname": "HighCPU", "service": "api"}, "values": {"B": 1}},
        session_id="session-1",
        task_id="celery-1",
        updated_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return ExistingIncident(**fields)


class TestAlertSignature:
    def test_metric_samples_and_annotation_text_are_not_identity(self):
        first = metadata_labels({"labels": {"alertname": "HighCPU"}, "values": {"B": 1}, "summary": "CPU is 91%"})
        second = metadata_labels({"labels": {"alertname": "HighCPU"}, "values": {"B": 99}, "summary": "CPU is 99%"})
        assert alert_signature("HighCPU", "api", "critical", first) == alert_signature("HighCPU", "api", "critical", second)

    def test_label_order_does_not_change_the_signature(self):
        first = alert_signature("HighCPU", "api", "critical", {"service": "api", "alertname": "HighCPU"})
        second = alert_signature("HighCPU", "api", "critical", {"alertname": "HighCPU", "service": "api"})
        assert first == second

    def test_metadata_json_string_parses(self):
        parsed = metadata_labels('{"labels": {"alertname": "HighCPU", "service": "api"}, "values": {"B": 1}}')
        assert alert_signature("HighCPU", "api", "critical", parsed) == _sig()


class TestRepeatNeedsInvestigation:
    def test_same_open_alert_does_not_need_another_rca(self):
        signature = _sig()
        assert repeat_needs_investigation("analyzed", signature, signature, session_id="s") is False
        assert repeat_needs_investigation("investigating", signature, signature, session_id="s", task_id="t") is False

    def test_resolved_incident_firing_again_needs_another_rca(self):
        signature = _sig()
        assert repeat_needs_investigation("resolved", signature, signature, session_id="s", task_id="t") is True

    def test_severity_service_or_label_change_needs_another_rca(self):
        assert repeat_needs_investigation("analyzed", _sig(severity="low"), _sig(severity="critical")) is True
        assert repeat_needs_investigation("analyzed", _sig(service="api"), _sig(service="worker")) is True
        before = _sig(labels={"alertname": "HighCPU", "pod": "a"})
        after = _sig(labels={"alertname": "HighCPU", "pod": "b"})
        assert repeat_needs_investigation("analyzed", before, after) is True

    def test_enqueue_that_never_landed_is_retried(self):
        signature = _sig()
        assert repeat_needs_investigation(
            "investigating", signature, signature, session_id=None, task_id=None,
        ) is True

    def test_a_real_task_id_without_a_session_is_already_enqueued(self):
        signature = _sig()
        assert repeat_needs_investigation(
            "investigating", signature, signature, session_id=None, task_id="celery-1",
        ) is False

    def test_fresh_enqueue_claim_blocks_a_second_investigation(self):
        signature = _sig()
        now = datetime.now(timezone.utc)
        assert repeat_needs_investigation(
            "investigating", signature, signature,
            session_id=None, task_id=RCA_ENQUEUE_CLAIM, updated_at=now, now=now,
        ) is False

    def test_stale_enqueue_claim_is_retried(self):
        signature = _sig()
        now = datetime.now(timezone.utc)
        claimed_at = now - timedelta(minutes=3)
        assert repeat_needs_investigation(
            "investigating", signature, signature,
            session_id=None, task_id=RCA_ENQUEUE_CLAIM, updated_at=claimed_at, now=now,
        ) is True


class TestShouldStartInvestigation:
    def test_a_new_incident_always_starts(self):
        assert should_start_investigation(True, None, title="HighCPU", service="api", severity="critical") is True

    def test_an_unobserved_conflict_does_not_start(self):
        assert should_start_investigation(False, None, title="HighCPU", service="api", severity="critical") is False

    def test_omitted_labels_do_not_look_like_a_changed_alert(self):
        existing = _existing(status="analyzed")
        assert should_start_investigation(
            False, existing, title="HighCPU", service="api", severity="critical",
        ) is False
