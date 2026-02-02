"""Tests for AlertCorrelator orchestrator."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.correlation.alert_correlator import AlertCorrelator, CorrelationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANDIDATE_ROW = (
    "inc-uuid-001",  # id
    "High CPU on api",  # alert_title
    "api-server",  # alert_service
    _NOW - timedelta(seconds=60),  # updated_at
)


def _make_cursor(rows=None, group_count=0):
    """Return a mock cursor that yields *rows* for the first SELECT and
    *group_count* for the incident_alerts COUNT query."""
    cursor = MagicMock(name="cursor")

    # We need to handle multiple execute() calls:
    #   1st: candidate incidents query
    #   2nd (optional): incident_alerts COUNT
    call_count = {"n": 0}
    original_rows = rows if rows is not None else []

    def _fetchall():
        return original_rows

    def _fetchone():
        # Called for the COUNT(*) query
        return (group_count,)

    cursor.fetchall = _fetchall
    cursor.fetchone = _fetchone
    return cursor


def _make_cursor_multi(candidate_rows, group_count=0):
    """Cursor that returns candidate_rows on fetchall, group_count on fetchone."""
    cursor = MagicMock(name="cursor")
    cursor.fetchall.return_value = candidate_rows
    cursor.fetchone.return_value = (group_count,)
    return cursor


# ---------------------------------------------------------------------------
# Environment variable defaults (clean slate)
# ---------------------------------------------------------------------------

_CLEAN_ENV = {
    "CORRELATION_ENABLED": "true",
    "CORRELATION_SHADOW_MODE": "false",
    "CORRELATION_TIME_WINDOW_SECONDS": "300",
    "CORRELATION_SCORE_THRESHOLD": "0.6",
    "CORRELATION_TOPOLOGY_WEIGHT": "0.5",
    "CORRELATION_TIME_WEIGHT": "0.3",
    "CORRELATION_SIMILARITY_WEIGHT": "0.2",
    "CORRELATION_MAX_GROUP_SIZE": "50",
}


def _env(**overrides):
    """Return env dict with overrides applied."""
    env = dict(_CLEAN_ENV)
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCorrelationResult:
    """Basic sanity checks for the dataclass."""

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
    """When no open incidents exist, correlation should not match."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    def test_no_candidates_returns_not_correlated(self):
        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(candidate_rows=[])
        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )
        assert result.is_correlated is False
        assert result.incident_id is None


class TestAlertCorrelatorAboveThreshold:
    """When strategies produce a combined score above the threshold."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_above_threshold_returns_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        # Configure strategy mocks to return high scores
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.8
        MockSimilarity.return_value.score.return_value = 0.7

        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(
            candidate_rows=[_CANDIDATE_ROW],
            group_count=5,
        )

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU on api",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        # weighted = 0.5*0.9 + 0.3*0.8 + 0.2*0.7 = 0.45+0.24+0.14 = 0.83
        assert result.is_correlated is True
        assert result.incident_id == "inc-uuid-001"
        assert result.score == pytest.approx(0.83)


class TestAlertCorrelatorBelowThreshold:
    """When strategies produce a combined score below the threshold."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_below_threshold_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        # Low scores → weighted below 0.6
        MockTopology.return_value.score.return_value = 0.1
        MockTimeWindow.return_value.score.return_value = 0.2
        MockSimilarity.return_value.score.return_value = 0.1

        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(candidate_rows=[_CANDIDATE_ROW])

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="Disk full",
            alert_service="storage",
            alert_severity="warning",
            alert_received_at=_NOW,
        )

        # weighted = 0.5*0.1 + 0.3*0.2 + 0.2*0.1 = 0.05+0.06+0.02 = 0.13
        assert result.is_correlated is False


class TestAlertCorrelatorShadowMode:
    """Shadow mode logs but returns not-correlated."""

    @patch.dict("os.environ", _env(CORRELATION_SHADOW_MODE="true"), clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_shadow_mode_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(
            candidate_rows=[_CANDIDATE_ROW],
            group_count=5,
        )

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU on api",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        # High scores but shadow → not correlated
        assert result.is_correlated is False


class TestAlertCorrelatorStrategyException:
    """A strategy raising an exception should not crash the orchestrator."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_strategy_exception_degrades_gracefully(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        # Topology raises, others return decent scores
        MockTopology.return_value.score.side_effect = RuntimeError("memgraph down")
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(
            candidate_rows=[_CANDIDATE_ROW],
            group_count=2,
        )

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU on api",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        # weighted = 0.5*0.0 + 0.3*0.9 + 0.2*0.9 = 0+0.27+0.18 = 0.45
        # 0.45 < 0.6 threshold → not correlated
        assert result.is_correlated is False

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_strategy_exception_with_high_remaining_scores(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        """Even with one strategy failing, high remaining scores can pass threshold."""
        MockTopology.return_value.score.side_effect = RuntimeError("boom")
        MockTimeWindow.return_value.score.return_value = 1.0
        MockSimilarity.return_value.score.return_value = 1.0

        correlator = AlertCorrelator()
        cursor = _make_cursor_multi(
            candidate_rows=[_CANDIDATE_ROW],
            group_count=2,
        )

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU on api",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        # weighted = 0.5*0.0 + 0.3*1.0 + 0.2*1.0 = 0+0.3+0.2 = 0.5
        # 0.5 < 0.6 → still not correlated
        assert result.is_correlated is False


class TestAlertCorrelatorMaxGroupSize:
    """When an incident already has max alerts, skip it."""

    @patch.dict("os.environ", _env(CORRELATION_MAX_GROUP_SIZE="5"), clear=False)
    @patch("services.correlation.alert_correlator.TopologyStrategy")
    @patch("services.correlation.alert_correlator.TimeWindowStrategy")
    @patch("services.correlation.alert_correlator.SimilarityStrategy")
    def test_max_group_size_returns_not_correlated(
        self, MockSimilarity, MockTimeWindow, MockTopology
    ):
        MockTopology.return_value.score.return_value = 0.9
        MockTimeWindow.return_value.score.return_value = 0.9
        MockSimilarity.return_value.score.return_value = 0.9

        correlator = AlertCorrelator()
        # group_count >= max_group_size (5)
        cursor = _make_cursor_multi(
            candidate_rows=[_CANDIDATE_ROW],
            group_count=5,
        )

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU on api",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        assert result.is_correlated is False


class TestAlertCorrelatorDisabled:
    """When CORRELATION_ENABLED is false, always return not-correlated."""

    @patch.dict("os.environ", _env(CORRELATION_ENABLED="false"), clear=False)
    def test_disabled_returns_not_correlated(self):
        correlator = AlertCorrelator()
        cursor = MagicMock()

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        assert result.is_correlated is False
        # Should not even query for candidates
        cursor.execute.assert_not_called()


class TestAlertCorrelatorUnexpectedError:
    """Any unexpected error in correlate() returns not-correlated."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    def test_db_error_returns_not_correlated(self):
        correlator = AlertCorrelator()
        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("DB connection lost")

        result = correlator.correlate(
            cursor=cursor,
            user_id="user-1",
            alert_title="High CPU",
            alert_service="api-server",
            alert_severity="critical",
            alert_received_at=_NOW,
        )

        assert result.is_correlated is False


class TestGetCandidateIncidents:
    """Tests for _get_candidate_incidents helper."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
    def test_query_parameters(self):
        """Verify the SQL query uses correct user_id and cutoff."""
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
        assert params[0] == "user-42"
        # cutoff should be _NOW - 300s
        expected_cutoff = _NOW - timedelta(seconds=300)
        assert params[1] == expected_cutoff


class TestScoreCandidate:
    """Tests for _score_candidate helper."""

    @patch.dict("os.environ", _CLEAN_ENV, clear=False)
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
            "updated_at": _NOW - timedelta(seconds=30),
        }

        result = correlator._score_candidate(
            candidate=candidate,
            user_id="user-1",
            alert_title="CPU alert",
            alert_service="api",
            alert_received_at=_NOW,
        )

        # All scores 1.0 → weighted = 0.5+0.3+0.2 = 1.0
        assert result.score == pytest.approx(1.0)
        assert result.incident_id == "inc-123"
        assert result.is_correlated is True
