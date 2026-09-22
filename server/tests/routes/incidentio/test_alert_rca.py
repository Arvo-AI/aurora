"""Tests for incident.io alert-event RCA support and severity filtering.

These cover the behavior added so that incident.io *alert* events
(``public_alert.*`` and ``private_alert.*``) — not just declared incidents —
get stored and can trigger RCA, with per-org customizable severity gating.

The production ``routes.incidentio.tasks`` module pulls in the heavy
LangGraph-backed background-chat stack at import time. To keep these unit
tests hermetic (no DB/Docker/LLM) we stub those import-time dependencies
before importing the module, mirroring the conftest approach for other
heavy packages.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch


def _install_import_stubs() -> None:
    """Stub the heavy modules ``tasks.py`` imports at module load time.

    Only the genuinely heavy, celery-backed imports are stubbed. We must NOT
    stub ``services.correlation.*`` here: those modules import fine on their
    own, and replacing them in ``sys.modules`` would poison the real modules
    for every other test in the session (e.g. the correlation suite would then
    fail to import ``bump_incident_alert_stats``/``CorrelationResult`` from a
    stub that never defined them).
    """
    # build_rca_prompt lives behind the LangGraph-heavy chat.background package.
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)

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

    def test_priority_from_alert_priority_attribute(self):
        # incident.io surfaces the resolved priority as a structured attribute
        # (type=AlertPriority) with the name in value.label — not in metadata.
        payload = {
            "event_type": "public_alert.alert_created_v1",
            "public_alert.alert_created_v1": {
                "id": "alert_attr_1",
                "title": "Attr priority alert",
                "status": "firing",
                "attributes": [
                    {
                        "value": {"label": "Urgent", "literal": "01ABC"},
                        "attribute": {"name": "Priority", "type": "AlertPriority"},
                    },
                    {
                        "value": {"label": "api"},
                        "attribute": {"name": "Service", "type": "String"},
                    },
                ],
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is True
        assert fields["severity"] == "Urgent"

    def test_attribute_priority_preferred_over_metadata(self):
        # When both exist, the structured AlertPriority attribute wins.
        payload = {
            "event_type": "public_alert.alert_created_v1",
            "event": {
                "alert": {
                    "id": "alert_attr_2",
                    "title": "Both sources",
                    "metadata": {"priority": "In-hours"},
                    "attributes": [
                        {
                            "value": {"label": "Urgent"},
                            "attribute": {"name": "Priority", "type": "AlertPriority"},
                        }
                    ],
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["severity"] == "Urgent"

    def test_private_alert_event_parsed_like_public(self):
        payload = {
            "event_type": "private_alert.alert_created_v1",
            "event": {
                "alert": {
                    "id": "alert_priv_1",
                    "title": "Private disk pressure",
                    "description": "Disk > 90%",
                    "status": "firing",
                    "source_url": "https://app.incident.io/alerts/alert_priv_1",
                    "metadata": {"severity": "high", "service": "db"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is True
        assert fields["incident_id"] == "alert_priv_1"
        assert fields["incident_name"] == "Private disk pressure"
        assert fields["severity"] == "high"

    def test_private_alert_keyed_by_topic_name(self):
        # Some events nest the alert under the topic key rather than event.alert.
        payload = {
            "event_type": "private_alert.alert_created_v1",
            "private_alert.alert_created_v1": {
                "alert": {
                    "id": "alert_priv_2",
                    "title": "Keyed private alert",
                    "metadata": {"severity": "critical"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is True
        assert fields["incident_id"] == "alert_priv_2"
        assert fields["incident_name"] == "Keyed private alert"
        assert fields["severity"] == "critical"

    def test_private_alert_created_triggers_rca(self):
        # The trigger gate must treat private alerts identically to public ones.
        assert "private_alert.alert_created_v1" in tasks._NEW_INCIDENT_EVENTS
        assert "public_alert.alert_created_v1" in tasks._NEW_INCIDENT_EVENTS

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

    def test_private_incident_keyed_by_topic_name(self):
        # Private declared incidents arrive keyed by their topic name rather
        # than under event.incident — they must be resolved like public ones,
        # not dropped as "no extractable identifier".
        payload = {
            "event_type": "private_incident.incident_created_v2",
            "private_incident.incident_created_v2": {
                "incident": {
                    "id": "inc_priv_1",
                    "name": "Private outage",
                    "status": "open",
                    "severity": {"name": "critical"},
                }
            },
        }
        fields = tasks._extract_incident_fields(payload)

        assert fields["is_alert"] is False
        assert fields["incident_id"] == "inc_priv_1"
        assert fields["incident_name"] == "Private outage"
        assert fields["severity"] == "critical"

    def test_private_incident_created_triggers_rca(self):
        # The trigger gate must treat private incidents identically to public.
        assert "private_incident.incident_created_v2" in tasks._NEW_INCIDENT_EVENTS
        assert "public_incident.incident_created_v2" in tasks._NEW_INCIDENT_EVENTS


# ---------------------------------------------------------------------------
# _severity_passes_filter — customizable severity gating
# ---------------------------------------------------------------------------
class TestSeverityFilter:
    def _prefs(self, mapping):
        """Return a get_user_preference stub backed by ``mapping``."""
        def _get(user_id, key, default=None):
            return mapping.get(key, default)
        return _get

    def _no_org_catalog(self):
        """Patch the org-severity fetch to empty so these tests stay hermetic."""
        return patch.object(tasks, "_get_org_severity_ranks", return_value={})

    def test_default_threshold_allows_all(self):
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs({})), \
             self._no_org_catalog():
            assert tasks._severity_passes_filter("u1", "low") is True
            assert tasks._severity_passes_filter("u1", "critical") is True

    def test_min_severity_high_filters_low_and_medium(self):
        prefs = {"incidentio_alert_min_severity": "high"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             self._no_org_catalog():
            assert tasks._severity_passes_filter("u1", "low") is False
            assert tasks._severity_passes_filter("u1", "medium") is False
            assert tasks._severity_passes_filter("u1", "high") is True
            assert tasks._severity_passes_filter("u1", "critical") is True

    def test_unknown_severity_is_never_filtered(self):
        prefs = {"incidentio_alert_min_severity": "critical"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             self._no_org_catalog():
            # Ambiguous severity should fail open so we don't silently drop it.
            assert tasks._severity_passes_filter("u1", "unknown") is True

    def test_allowlist_overrides_min_severity(self):
        prefs = {
            "incidentio_alert_min_severity": "critical",
            "incidentio_alert_severity_allowlist": ["low", "critical"],
        }
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             self._no_org_catalog():
            # Explicit allowlist wins: low is allowed even though min is critical.
            assert tasks._severity_passes_filter("u1", "low") is True
            assert tasks._severity_passes_filter("u1", "critical") is True
            # Not in allowlist -> filtered.
            assert tasks._severity_passes_filter("u1", "high") is False
            assert tasks._severity_passes_filter("u1", "medium") is False

    def test_invalid_min_severity_fails_open(self):
        prefs = {"incidentio_alert_min_severity": "bogus"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             self._no_org_catalog():
            assert tasks._severity_passes_filter("u1", "high") is True


# ---------------------------------------------------------------------------
# Custom (org-defined) severities — resolved via the org's severity catalog
# ---------------------------------------------------------------------------
class TestNormalizeViaOrgRank:
    # A typical 4-severity catalog: Minor(1) < Degraded(2) < Major(3) < Critical(4)
    _RANKS = {"minor": 1, "degraded": 2, "major": 3, "critical": 4}

    def test_bottom_of_range_is_low(self):
        assert tasks._normalize_via_org_rank("Minor", self._RANKS) == "low"

    def test_top_of_range_is_critical(self):
        assert tasks._normalize_via_org_rank("Critical", self._RANKS) == "critical"

    def test_custom_mid_severity_buckets_reasonably(self):
        # "Major" sits high in the range -> should not be treated as noise.
        assert tasks._normalize_via_org_rank("Major", self._RANKS) in ("high", "critical")

    def test_name_not_in_catalog_returns_none(self):
        assert tasks._normalize_via_org_rank("Nonexistent", self._RANKS) is None

    def test_empty_catalog_returns_none(self):
        assert tasks._normalize_via_org_rank("Minor", {}) is None

    def test_single_severity_defaults_high(self):
        # No gradient possible — don't accidentally classify as low noise.
        assert tasks._normalize_via_org_rank("OnlyOne", {"onlyone": 1}) == "high"


class TestCustomSeverityFilter:
    def _prefs(self, mapping):
        def _get(user_id, key, default=None):
            return mapping.get(key, default)
        return _get

    def test_custom_severity_resolved_and_filtered_out(self):
        # min=high; a low custom severity ("Minor") should be filtered out
        # instead of always passing as "unknown".
        prefs = {"incidentio_alert_min_severity": "high"}
        ranks = {"minor": 1, "degraded": 2, "major": 3, "critical": 4}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value=ranks):
            # normalized is "unknown" (custom name), but raw resolves to "low".
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Minor") is False

    def test_custom_severity_resolved_and_passes(self):
        prefs = {"incidentio_alert_min_severity": "high"}
        ranks = {"minor": 1, "degraded": 2, "major": 3, "critical": 4}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value=ranks):
            # "Critical" is top of range -> clears a high threshold.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Critical") is True

    def test_custom_severity_not_in_catalog_fails_open(self):
        prefs = {"incidentio_alert_min_severity": "critical"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value={"minor": 1, "major": 2}):
            # Unrecognized custom name -> stays "unknown" -> never dropped.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Mystery") is True

    def test_catalog_unavailable_fails_open(self):
        prefs = {"incidentio_alert_min_severity": "critical"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value={}):
            # No catalog (API error) -> fail open, don't drop the alert.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Degraded") is True

    def test_raw_custom_name_matches_allowlist(self):
        # Orgs can allowlist a custom severity label directly by name.
        prefs = {"incidentio_alert_severity_allowlist": ["degraded"]}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value={}):
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Degraded") is True
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Cosmetic") is False


# ---------------------------------------------------------------------------
# Real org severity *name* as the minimum-severity threshold (rank compare)
# ---------------------------------------------------------------------------
class TestRealNameThreshold:
    def _prefs(self, mapping):
        def _get(user_id, key, default=None):
            return mapping.get(key, default)
        return _get

    # Minor(1) < Degraded(2) < Major(3) < Critical(4)
    _RANKS = {"minor": 1, "degraded": 2, "major": 3, "critical": 4}

    def test_threshold_by_real_name_filters_below(self):
        prefs = {"incidentio_alert_min_severity": "major"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value=self._RANKS):
            # "Degraded" (rank 2) is below "Major" (rank 3) -> filtered.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Degraded") is False
            # "Major" itself clears the threshold.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Major") is True
            # "Critical" (rank 4) is above -> passes.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Critical") is True

    def test_alert_severity_not_in_catalog_fails_open_against_real_threshold(self):
        prefs = {"incidentio_alert_min_severity": "major"}
        with patch("utils.auth.stateless_auth.get_user_preference", self._prefs(prefs)), \
             patch.object(tasks, "_get_org_severity_ranks", return_value=self._RANKS):
            # Unknown alert severity vs a real-name threshold -> fail open.
            assert tasks._severity_passes_filter("u1", "unknown", raw_severity="Mystery") is True


# ---------------------------------------------------------------------------
# get_org_severities — structured catalog + availability for the UI
# ---------------------------------------------------------------------------
class TestGetOrgSeverities:
    def test_available_when_catalog_present(self):
        # Alert Priority catalog ranks: higher = more urgent (Urgent 100).
        ranks = {"in-hours": 98, "urgent": 100}
        with patch.object(tasks, "_get_org_severity_ranks", return_value=ranks), \
             patch("utils.cache.redis_client.get_redis_client", return_value=None):
            result = tasks.get_org_severities("u1")

        assert result["available"] is True
        # Sorted most-urgent first.
        names = [s["name"] for s in result["severities"]]
        assert names == ["urgent", "in-hours"]
        # Rank surfaced as-is (higher = more urgent).
        assert result["severities"][0] == {"name": "urgent", "rank": 100}

    def test_unavailable_when_empty(self):
        with patch.object(tasks, "_get_org_severity_ranks", return_value={}), \
             patch("utils.cache.redis_client.get_redis_client", return_value=None):
            result = tasks.get_org_severities("u1")

        assert result["available"] is False
        assert result["severities"] == []


# ---------------------------------------------------------------------------
# _get_org_severity_ranks — fetching/caching the org's severity catalog,
# including handling API keys that lack the "View data" scope (403).
# ---------------------------------------------------------------------------
class _FakeRedis:
    """Minimal in-memory Redis stand-in for get/set assertions."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


class TestGetOrgSeverityRanks:
    def _import_error(self):
        from routes.incidentio.incidentio_routes import IncidentioAPIError
        return IncidentioAPIError

    def test_success_parses_and_caches_catalog(self):
        rc = _FakeRedis()
        client = MagicMock()
        # Alert Priority catalog entries: higher rank = more urgent.
        client.list_alert_priorities.return_value = {
            "severities": [
                {"name": "In-hours", "rank": 98},
                {"name": "Urgent", "rank": 100},
            ]
        }
        with patch("utils.cache.redis_client.get_redis_client", return_value=rc), \
             patch("utils.auth.token_management.get_token_data", return_value={"api_key": "k"}), \
             patch("routes.incidentio.incidentio_routes.IncidentioClient", return_value=client):
            ranks = tasks._get_org_severity_ranks("u1")

        # Ranks kept as-is (higher = more urgent), no inversion.
        assert ranks == {"in-hours": 98, "urgent": 100}
        # Cached value is the JSON catalog, not the denied marker.
        cached = rc.store["incidentio:severity_ranks:u1"]
        assert cached != tasks._ORG_SEVERITY_DENIED_MARKER
        assert "urgent" in cached

    def test_forbidden_caches_denied_marker(self):
        rc = _FakeRedis()
        IncidentioAPIError = self._import_error()
        client = MagicMock()
        client.list_alert_priorities.side_effect = IncidentioAPIError(IncidentioAPIError.FORBIDDEN)
        with patch("utils.cache.redis_client.get_redis_client", return_value=rc), \
             patch("utils.auth.token_management.get_token_data", return_value={"api_key": "k"}), \
             patch("routes.incidentio.incidentio_routes.IncidentioClient", return_value=client):
            ranks = tasks._get_org_severity_ranks("u1")

        assert ranks == {}
        # 403 is permanent for this key — cache the denied marker to stop retrying.
        assert rc.store["incidentio:severity_ranks:u1"] == tasks._ORG_SEVERITY_DENIED_MARKER

    def test_denied_marker_short_circuits_without_api_call(self):
        rc = _FakeRedis()
        rc.store["incidentio:severity_ranks:u1"] = tasks._ORG_SEVERITY_DENIED_MARKER
        client = MagicMock()
        with patch("utils.cache.redis_client.get_redis_client", return_value=rc), \
             patch("utils.auth.token_management.get_token_data", return_value={"api_key": "k"}), \
             patch("routes.incidentio.incidentio_routes.IncidentioClient", return_value=client):
            ranks = tasks._get_org_severity_ranks("u1")

        assert ranks == {}
        # Cached denial means we must NOT hit the API again.
        client.list_alert_priorities.assert_not_called()

    def test_transient_error_is_not_cached(self):
        rc = _FakeRedis()
        IncidentioAPIError = self._import_error()
        client = MagicMock()
        client.list_alert_priorities.side_effect = IncidentioAPIError(IncidentioAPIError.TIMEOUT)
        with patch("utils.cache.redis_client.get_redis_client", return_value=rc), \
             patch("utils.auth.token_management.get_token_data", return_value={"api_key": "k"}), \
             patch("routes.incidentio.incidentio_routes.IncidentioClient", return_value=client):
            ranks = tasks._get_org_severity_ranks("u1")

        assert ranks == {}
        # Transient failure must not poison the cache — next alert should retry.
        assert "incidentio:severity_ranks:u1" not in rc.store


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
