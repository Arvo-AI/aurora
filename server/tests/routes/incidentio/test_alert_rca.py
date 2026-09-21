"""Tests for incident.io alert-event RCA support and severity filtering.

These cover the behavior added so that incident.io *alert* events
(``public_alert.*``) — not just declared incidents — get stored and can
trigger RCA, with per-org customizable severity gating.

The production ``routes.incidentio.tasks`` module pulls in the heavy
LangGraph-backed background-chat stack at import time. To keep these unit
tests hermetic (no DB/Docker/LLM) we stub those import-time dependencies
before importing the module, mirroring the conftest approach for other
heavy packages.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_import_stubs() -> None:
    """Stub the heavy modules ``tasks.py`` imports at module load time."""
    # build_rca_prompt lives behind the LangGraph-heavy chat.background package.
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)

    # services.correlation.* — only the symbols tasks.py imports.
    corr_pkg = sys.modules.get("services.correlation") or ModuleType("services.correlation")
    corr_pkg.apply_correlation_outcome = lambda *a, **k: False  # type: ignore[attr-defined]
    sys.modules["services.correlation"] = corr_pkg

    corr_mod = ModuleType("services.correlation.alert_correlator")
    corr_mod.AlertCorrelator = MagicMock  # type: ignore[attr-defined]
    sys.modules["services.correlation.alert_correlator"] = corr_mod

    # celery_config.celery_app must expose a .task decorator that returns the fn.
    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                return fn
            # Support both @task and @task(...) usage.
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    sys.modules.setdefault("celery_config", celery_cfg)


_install_import_stubs()

from routes.incidentio import tasks  # noqa: E402


# ---------------------------------------------------------------------------
# _extract_incident_fields — alert event parsing
# ---------------------------------------------------------------------------
class TestExtractAlertFields:
    def test_alert_event_yields_stable_id_from_alert_id(self):
        payload = {
            "event_type": "public_alert.alert_created_v1",
            "event": {
                "alert": {
                    "id": "alert_123",
                    "title": "High CPU on api",
                    "description": "CPU exceeded 95%",
                    "status": "firing",
                    "source_url": "https://app.incident.io/alerts/alert_123",
                    "metadata": {"severity": "critical", "service": "api"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is True
        # No incident_id on alerts — we synthesize a stable ref for dedup.
        assert fields["incident_id"] == "alert_123"
        assert fields["incident_name"] == "High CPU on api"
        assert fields["severity"] == "critical"
        assert fields["summary"] == "CPU exceeded 95%"
        assert fields["permalink"] == "https://app.incident.io/alerts/alert_123"

    def test_alert_event_falls_back_to_dedup_key(self):
        payload = {
            "event_type": "public_alert.alert_created_v1",
            "event": {
                "alert": {
                    "deduplication_key": "dedup-999",
                    "title": "Latency spike",
                    "metadata": {"priority": "high"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is True
        assert fields["incident_id"] == "dedup-999"
        assert fields["severity"] == "high"

    def test_incident_event_is_not_flagged_as_alert(self):
        payload = {
            "event_type": "incident.created",
            "event": {
                "incident": {
                    "id": "inc_1",
                    "name": "Checkout down",
                    "status": "open",
                    "severity": {"name": "critical"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is False
        assert fields["incident_id"] == "inc_1"
        assert fields["incident_name"] == "Checkout down"


# ---------------------------------------------------------------------------
# _severity_passes_filter — customizable severity gating
# ---------------------------------------------------------------------------
class TestSeverityFilter:
    def _prefs(self, mapping):
        """Return a get_user_preference stub backed by ``mapping``."""
        def _get(user_id, key, default=None):
            return mapping.get(key, default)
        return _get

    def test_default_threshold_allows_all(self):
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs({})):
            assert tasks._severity_passes_filter("u1", "low") is True
            assert tasks._severity_passes_filter("u1", "critical") is True

    def test_min_severity_high_filters_low_and_medium(self):
        prefs = {"incidentio_alert_min_severity": "high"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)):
            assert tasks._severity_passes_filter("u1", "low") is False
            assert tasks._severity_passes_filter("u1", "medium") is False
            assert tasks._severity_passes_filter("u1", "high") is True
            assert tasks._severity_passes_filter("u1", "critical") is True

    def test_unknown_severity_is_never_filtered(self):
        prefs = {"incidentio_alert_min_severity": "critical"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)):
            # Ambiguous severity should fail open so we don't silently drop it.
            assert tasks._severity_passes_filter("u1", "unknown") is True

    def test_allowlist_overrides_min_severity(self):
        prefs = {
            "incidentio_alert_min_severity": "critical",
            "incidentio_alert_severity_allowlist": ["low", "critical"],
        }
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)):
            # Explicit allowlist wins: low is allowed even though min is critical.
            assert tasks._severity_passes_filter("u1", "low") is True
            assert tasks._severity_passes_filter("u1", "critical") is True
            # Not in allowlist -> filtered.
            assert tasks._severity_passes_filter("u1", "high") is False
            assert tasks._severity_passes_filter("u1", "medium") is False

    def test_invalid_min_severity_fails_open(self):
        prefs = {"incidentio_alert_min_severity": "bogus"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)):
            assert tasks._severity_passes_filter("u1", "high") is True


# ---------------------------------------------------------------------------
# _should_trigger_alert_rca — alert RCA opt-in
# ---------------------------------------------------------------------------
class TestAlertRcaToggle:
    def test_defaults_to_true(self):
        with patch("utils.auth.stateless_auth.get_user_preference",
                   lambda uid, key, default=None: default):
            assert tasks._should_trigger_alert_rca("u1") is True

    def test_respects_disabled_pref(self):
        with patch("utils.auth.stateless_auth.get_user_preference",
                   lambda uid, key, default=None: False):
            assert tasks._should_trigger_alert_rca("u1") is False
