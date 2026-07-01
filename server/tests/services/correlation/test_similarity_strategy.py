"""Tests for SimilarityStrategy."""

import pytest

from services.correlation.strategies.similarity import SimilarityStrategy


class TestSimilarityStrategyScoring:
    """Tests for the Jaccard-based scoring logic."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    def test_identical_texts_high_score(self):
        """Identical titles and matching service should score ~1.0."""
        score = self.strategy.score(
            alert_title="High CPU usage on payment service",
            alert_service="payment-service",
            incident_title="High CPU usage on payment service",
            incident_services=["payment-service"],
        )
        assert score >= 0.95

    def test_partial_overlap_mid_score(self):
        """Partial overlap in titles gives mid-range score."""
        score = self.strategy.score(
            alert_title="High memory usage on api-server",
            alert_service="api-server",
            incident_title="High CPU usage on api-server",
            incident_services=["api-server"],
        )
        assert 0.3 <= score <= 0.9

    def test_different_texts_low_score(self):
        """Unrelated texts should have low similarity."""
        score = self.strategy.score(
            alert_title="Disk space critically low",
            alert_service="storage-node",
            incident_title="Network latency spike",
            incident_services=["network-gateway"],
        )
        assert score <= 0.3


class TestSimilarityStrategyServiceMatching:
    """Tests for service name matching."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    def test_exact_service_match_boosts_score(self):
        """Exact service match gives full service score."""
        score = self.strategy.score(
            alert_title="Error rate spike",
            alert_service="checkout-service",
            incident_title="Error rate spike",
            incident_services=["checkout-service"],
        )
        assert score == pytest.approx(1.0)

    def test_different_service_reduces_score(self):
        """Different service name reduces the service component."""
        score_match = self.strategy.score(
            alert_title="Error rate spike",
            alert_service="checkout-service",
            incident_title="Error rate spike",
            incident_services=["checkout-service"],
        )
        score_diff = self.strategy.score(
            alert_title="Error rate spike",
            alert_service="checkout-service",
            incident_title="Error rate spike",
            incident_services=["payment-gateway"],
        )
        assert score_match > score_diff


class TestSimilarityStrategyEdgeCases:
    """Edge case tests."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    def test_empty_alert_title_returns_zero(self):
        """Empty alert title returns 0.0."""
        score = self.strategy.score(
            alert_title="",
            alert_service="svc",
            incident_title="Something happened",
            incident_services=["svc"],
        )
        assert score == 0.0

    def test_empty_incident_title_returns_zero(self):
        """Empty incident title returns 0.0."""
        score = self.strategy.score(
            alert_title="Something happened",
            alert_service="svc",
            incident_title="",
            incident_services=["svc"],
        )
        assert score == 0.0

    def test_empty_service_still_scores_on_title(self):
        """Empty services should not crash; score uses title only."""
        score = self.strategy.score(
            alert_title="CPU overload detected",
            alert_service="",
            incident_title="CPU overload detected",
            incident_services=[],
        )
        assert score == pytest.approx(0.7)


class TestJaccardSimilarity:
    """Tests for Jaccard tokenization and similarity."""

    def test_stopwords_removed(self):
        """Stopwords should not contribute to similarity."""
        tokens = SimilarityStrategy._tokenize("the a an is are was for in on at")
        assert len(tokens) == 0

    def test_short_tokens_removed(self):
        """Single-char tokens are filtered out."""
        tokens = SimilarityStrategy._tokenize("a b c db")
        assert tokens == {"db"}

    def test_tokenize_extracts_alphanumeric(self):
        """Tokeniser extracts [a-z0-9]+ tokens, lowercased."""
        tokens = SimilarityStrategy._tokenize("HTTP-500 Error on API_v2!")
        assert "http" in tokens
        assert "500" in tokens
        assert "error" in tokens
        assert "api" in tokens
        assert "v2" in tokens

    def test_jaccard_identical_sets(self):
        """Identical sets have Jaccard index of 1.0."""
        sim = SimilarityStrategy._jaccard({"a", "b"}, {"a", "b"})
        assert sim == 1.0

    def test_jaccard_disjoint_sets(self):
        """Disjoint sets have Jaccard index of 0.0."""
        sim = SimilarityStrategy._jaccard({"a", "b"}, {"c", "d"})
        assert sim == 0.0

    def test_jaccard_partial_overlap(self):
        """Partial overlap gives mid-range Jaccard."""
        sim = SimilarityStrategy._jaccard({"a", "b", "c"}, {"b", "c", "d"})
        assert sim == pytest.approx(0.5)
