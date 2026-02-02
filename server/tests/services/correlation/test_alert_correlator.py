"""Tests for AlertCorrelator orchestrator."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.correlation.alert_correlator import AlertCorrelator, CorrelationResult


_NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANDIDATE_ROW = (
    "inc-uuid-001",
    "High CPU on api",
    "api-server",
    ["api-server"],
    3,
    _NOW - timedelta(hours=1),
    _NOW - timedelta(seconds=60),
)


def _make_candidate_row(
    id="inc-uuid-001",
    title="High CPU on api",
    service="api-server",
    affected=None,
    count=3,
    started_at=None,
    updated_at=None,
):
    return (
        id,
        title,
        service,
        affected if affected is not None else [service],
        count,
        started_at or _NOW - timedelta(hours=1),
        updated_at or _NOW - timedelta(seconds=60),
    )


def _make_cursor(candidate_rows):
    cursor = MagicMock(name="cursor")
    cursor.fetchall.return_value = candidate_rows
    return cursor


_ALERT_META = {"received_at": _NOW}


def _call_correlate(correlator, cursor, **overrides):
    kwargs = dict(
        cursor=cursor,
        user_id="user-1",
        source_type="grafana",
        source_alert_id=42,
        alert_title="High CPU on api",
        alert_service="api-server",
        alert_severity="critical",
        alert_metadata=_ALERT_META,
    )
    kwargs.update(overrides)
    return correlator.correlate(**kwargs)


class TestCorrelationResult:
    def test_defaults(self):
        r = CorrelationResult(is_correlated=False)
        assert r.is_correlated is False
        assert r.incident_id is None
        assert r.score == 0.0
        assert r.strategy == ""
        assert r.details == {}

    def test_populated(self):
        r = CorrelationResult(
            is_correlated=True,
            incident_id="abc",
            score=0.85,
            strategy="topology",
            details={"topology": 0.9},
        )
        assert r.is_correlated is True
        assert r.incident_id == "abc"


class TestAlertCorrelatorNoCandidates:
    def test_no_candidates_returns_not_correlated(self):
        correlator = AlertCorrelator()
        cursor = _make_cursor(candidate_rows=[])
        result = _call_correlate(correlator, cursor)
        assert result.is_correlated is False
        assert result.incident_id is None


class TestAlertCorrelatorAboveThreshold:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_above_threshold_returns_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.8
        MockSimilarity.return_value.score.return_value = 0.7

        correlator = AlertCorrelator()
        cursor = _make_cursor(candidate_rows=[_CANDIDATE_ROW])

        result = _call_correlate(correlator, cursor)

        # weighted = 0.5*0.9 + 0.3*0.8 + 0.2*0.7 = 0.45+0.24+0.14 = 0.83
        assert result.is_correlated is True
        assert result.incident_id == "inc-uuid-001"
        assert result.score == pytest.approx(0.83)


class TestAlertCorrelatorBelowThreshold:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_below_threshold_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.1
        MockTimeWindow.return_value.score.return_value = 0.2
        MockSimilarity.return_value.score.return_value = 0.1

        correlator = AlertCorrelator()
        cursor = _make_cursor(candidate_rows=[_CANDIDATE_ROW])

        result = _call_correlate(
            correlator,
            cursor,
            alert_title="Disk full",
            alert_service="storage",
            alert_severity="warning",
        )

        # weighted = 0.5*0.1 + 0.3*0.2 + 0.2*0.1 = 0.05+0.06+0.02 = 0.13
        assert result.is_correlated is False


class TestAlertCorrelatorShadowMode:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_shadow_mode_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator(shadow_mode=True)
        cursor = _make_cursor(candidate_rows=[_CANDIDATE_ROW])

        result = _call_correlate(correlator, cursor)

        assert result.is_correlated is False


class TestAlertCorrelatorStrategyException:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_strategy_exception_degrades_gracefully(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.side_effect = RuntimeError("memgraph down")
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator()
        cursor = _make_cursor(candidate_rows=[_CANDIDATE_ROW])

        result = _call_correlate(correlator, cursor)

        # weighted = 0.5*0.0 + 0.3*0.9 + 0.2*0.9 = 0+0.27+0.18 = 0.45
        # 0.45 < 0.6 threshold
        assert result.is_correlated is False

    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_strategy_exception_with_high_remaining_scores(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.side_effect = RuntimeError("boom")
        MockTimeWindow.return_value.score.return_value = 1.0
        MockSimilarity.return_value.score.return_value = 1.0

        correlator = AlertCorrelator()
        cursor = _make_cursor(candidate_rows=[_CANDIDATE_ROW])

        result = _call_correlate(correlator, cursor)

        # weighted = 0.5*0.0 + 0.3*1.0 + 0.2*1.0 = 0+0.3+0.2 = 0.5
        # 0.5 < 0.6
        assert result.is_correlated is False


class TestAlertCorrelatorMaxGroupSize:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_max_group_size_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator(max_group_size=5)
        row = _make_candidate_row(count=5)
        cursor = _make_cursor(candidate_rows=[row])

        result = _call_correlate(correlator, cursor)

        assert result.is_correlated is False


class TestAlertCorrelatorDisabled:
    def test_disabled_returns_not_correlated(self):
        correlator = AlertCorrelator(enabled=False)
        cursor = MagicMock()

        result = _call_correlate(correlator, cursor)

        assert result.is_correlated is False
        cursor.execute.assert_not_called()


class TestAlertCorrelatorUnexpectedError:
    def test_db_error_returns_not_correlated(self):
        correlator = AlertCorrelator()
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("DB connection lost")

        result = _call_correlate(correlator, cursor)

        assert result.is_correlated is False


class TestResolveReceivedAt:
    def test_datetime_value(self):
        ts = datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        result = AlertCorrelator._resolve_received_at({"received_at": ts})
        assert result == ts

    def test_iso_string(self):
        result = AlertCorrelator._resolve_received_at(
            {"received_at": "2026-01-15T08:00:00+00:00"}
        )
        assert result == datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)

    def test_iso_string_with_z(self):
        result = AlertCorrelator._resolve_received_at(
            {"received_at": "2026-01-15T08:00:00Z"}
        )
        assert result == datetime(2026, 1, 15, 8, 0, 0, tzinfo=timezone.utc)

    def test_none_metadata_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        result = AlertCorrelator._resolve_received_at(None)
        after = datetime.now(timezone.utc)
        assert before <= result <= after

    def test_missing_key_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        result = AlertCorrelator._resolve_received_at({"other": "data"})
        after = datetime.now(timezone.utc)
        assert before <= result <= after


class TestGetCandidateIncidents:
    def test_query_parameters(self):
        correlator = AlertCorrelator()
        cursor = MagicMock()
        cursor.fetchall.return_value = []

        correlator._get_candidate_incidents(cursor, "user-42", _NOW)

        cursor.execute.assert_called_once()
        args = cursor.execute.call_args
        sql = args[0][0]
        params = args[0][1]

        assert "status = 'investigating'" in sql
        assert "updated_at >=" in sql
        assert "LIMIT 20" in sql
        assert "affected_services" in sql
        assert "correlated_alert_count" in sql
        assert params[0] == "user-42"
        expected_cutoff = _NOW - timedelta(seconds=300)
        assert params[1] == expected_cutoff

    def test_null_affected_services_defaults_to_empty_list(self):
        correlator = AlertCorrelator()
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            ("id-1", "title", "svc", None, None, _NOW, _NOW),
        ]

        results = correlator._get_candidate_incidents(cursor, "user-1", _NOW)

        assert results[0]["affected_services"] == []
        assert results[0]["correlated_alert_count"] == 0


class TestScoreCandidate:
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_weighted_score_calculation(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 1.0
        MockTimeWindow.return_value.score.return_value = 1.0
        MockSimilarity.return_value.score.return_value = 1.0

        correlator = AlertCorrelator()
        candidate = {
            "id": "inc-123",
            "alert_title": "CPU alert",
            "alert_service": "api",
            "affected_services": ["api", "cache"],
            "correlated_alert_count": 2,
            "updated_at": _NOW - timedelta(seconds=30),
        }

        result = correlator._score_candidate(
            candidate=candidate,
            user_id="user-1",
            alert_title="CPU alert",
            alert_service="api",
            alert_received_at=_NOW,
        )

        # All scores 1.0 -> weighted = 0.5+0.3+0.2 = 1.0
        assert result.score == pytest.approx(1.0)
        assert result.incident_id == "inc-123"
        assert result.is_correlated is True
        assert result.details["correlated_alert_count"] == 2

    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_falls_back_to_alert_service_when_no_affected(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.5
        MockTimeWindow.return_value.score.return_value = 0.5
        MockSimilarity.return_value.score.return_value = 0.5

        correlator = AlertCorrelator()
        candidate = {
            "id": "inc-456",
            "alert_title": "Disk alert",
            "alert_service": "storage",
            "affected_services": [],
            "correlated_alert_count": 0,
            "updated_at": _NOW - timedelta(seconds=30),
        }

        correlator._score_candidate(
            candidate=candidate,
            user_id="user-1",
            alert_title="Disk alert",
            alert_service="storage",
            alert_received_at=_NOW,
        )

        MockTopology.return_value.score.assert_called_once_with(
            "storage",
            ["storage"],
            "user-1",
        )
