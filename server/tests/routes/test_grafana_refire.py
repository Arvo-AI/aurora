"""Repeat firings of the same Grafana alert must not start another RCA.

Metric samples and templated annotation text change on every evaluation.
Those are not a new problem. A resolved incident firing again, or a real
change in title, service, severity, or labels, is.
"""

import importlib.util
import os

# Load the module by path. Importing routes.grafana pulls in the Celery task
# stack, which connects to Redis at import time.
_REFIRE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "routes", "grafana", "refire.py")
)
_spec = importlib.util.spec_from_file_location("grafana_refire_under_test", _REFIRE_PATH)
_refire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_refire)

_stable_alert_signature = _refire._stable_alert_signature
refire_needs_new_rca = _refire.refire_needs_new_rca


def _sig(**overrides):
    base = dict(
        title="HighCPU",
        service="api",
        severity="critical",
        metadata={"labels": {"alertname": "HighCPU", "service": "api"}, "values": {"B": 1}},
    )
    base.update(overrides)
    return _stable_alert_signature(base["title"], base["service"], base["severity"], base["metadata"])


class TestStableAlertSignature:
    def test_metric_samples_do_not_change_the_signature(self):
        first = _sig(metadata={"labels": {"alertname": "HighCPU"}, "values": {"B": 1}, "summary": "CPU is 91%"})
        second = _sig(metadata={"labels": {"alertname": "HighCPU"}, "values": {"B": 99}, "summary": "CPU is 99%"})
        assert first == second

    def test_label_order_does_not_change_the_signature(self):
        first = _sig(metadata={"labels": {"service": "api", "alertname": "HighCPU"}})
        second = _sig(metadata={"labels": {"alertname": "HighCPU", "service": "api"}})
        assert first == second

    def test_metadata_json_string_parses(self):
        as_dict = _sig()
        as_text = _stable_alert_signature(
            "HighCPU",
            "api",
            "critical",
            '{"labels": {"alertname": "HighCPU", "service": "api"}, "values": {"B": 1}}',
        )
        assert as_dict == as_text


class TestRefireNeedsNewRca:
    def test_same_open_alert_does_not_need_another_rca(self):
        signature = _sig()
        assert refire_needs_new_rca("analyzed", signature, signature) is False
        assert refire_needs_new_rca("investigating", signature, signature) is False

    def test_resolved_incident_firing_again_needs_another_rca(self):
        signature = _sig()
        assert refire_needs_new_rca("resolved", signature, signature) is True

    def test_severity_change_needs_another_rca(self):
        assert refire_needs_new_rca("analyzed", _sig(severity="low"), _sig(severity="critical")) is True

    def test_service_change_needs_another_rca(self):
        assert refire_needs_new_rca("analyzed", _sig(service="api"), _sig(service="worker")) is True

    def test_label_change_needs_another_rca(self):
        before = _sig(metadata={"labels": {"alertname": "HighCPU", "pod": "a"}})
        after = _sig(metadata={"labels": {"alertname": "HighCPU", "pod": "b"}})
        assert refire_needs_new_rca("analyzed", before, after) is True
