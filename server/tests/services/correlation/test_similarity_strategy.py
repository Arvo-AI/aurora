"""Tests for SimilarityStrategy."""

from unittest.mock import MagicMock, patch

import pytest

from services.correlation.strategies.similarity import SimilarityStrategy


class TestSimilarityStrategyWithVectors:
    """Tests with mocked embedding client for vector similarity."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_identical_texts_high_cosine_similarity(self, mock_get_client):
        """Identical texts should have cosine similarity of 1.0."""
        mock_client = MagicMock()
        mock_client.embed.return_value = [0.5, 0.5, 0.5, 0.5]
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="High CPU usage on payment service",
            alert_service="payment-service",
            incident_title="High CPU usage on payment service",
            incident_services=["payment-service"],
        )
        assert score >= 0.95

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_similar_texts_high_score(self, mock_get_client):
        """Similar texts should have high cosine similarity."""
        mock_client = MagicMock()
        mock_client.embed.side_effect = [
            [0.8, 0.4, 0.2, 0.1],
            [0.75, 0.45, 0.25, 0.05],
        ]
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="High CPU usage on api server",
            alert_service="api-server",
            incident_title="CPU spike detected on api server",
            incident_services=["api-server"],
        )
        assert score >= 0.7

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_different_texts_low_score(self, mock_get_client):
        """Unrelated texts should have low cosine similarity."""
        mock_client = MagicMock()
        mock_client.embed.side_effect = [
            [0.9, 0.1, 0.0, 0.0],
            [0.0, 0.0, 0.1, 0.9],
        ]
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="Disk space critically low",
            alert_service="storage-node",
            incident_title="Network latency spike",
            incident_services=["network-gateway"],
        )
        assert score <= 0.3


class TestSimilarityStrategyFallback:
    """Tests for Jaccard fallback when embeddings unavailable."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_fallback_to_jaccard_on_embedding_failure(self, mock_get_client):
        """Falls back to Jaccard when embedding service unavailable."""
        mock_client = MagicMock()
        mock_client.embed.return_value = None
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="High CPU usage on payment service",
            alert_service="payment-service",
            incident_title="High CPU usage on payment service",
            incident_services=["payment-service"],
        )
        assert score >= 0.8

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_fallback_partial_overlap(self, mock_get_client):
        """Fallback Jaccard gives mid-range score for partial overlap."""
        mock_client = MagicMock()
        mock_client.embed.return_value = None
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="High memory usage on api-server",
            alert_service="api-server",
            incident_title="High CPU usage on api-server",
            incident_services=["api-server"],
        )
        assert 0.3 <= score <= 0.9


class TestSimilarityStrategyServiceMatching:
    """Tests for service name matching (independent of vector similarity)."""

    def setup_method(self):
        self.strategy = SimilarityStrategy()

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_exact_service_match_boosts_score(self, mock_get_client):
        """Exact service match gives full service score."""
        mock_client = MagicMock()
        mock_client.embed.return_value = [0.5, 0.5, 0.5, 0.5]
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="Error rate spike",
            alert_service="checkout-service",
            incident_title="Error rate spike",
            incident_services=["checkout-service"],
        )
        assert score == pytest.approx(1.0)

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_different_service_reduces_score(self, mock_get_client):
        """Different service name reduces the service component."""
        mock_client = MagicMock()
        mock_client.embed.return_value = [0.5, 0.5, 0.5, 0.5]
        mock_get_client.return_value = mock_client

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

    @patch("services.correlation.strategies.similarity.get_embedding_client")
    def test_empty_service_still_scores_on_title(self, mock_get_client):
        """Empty services should not crash; score uses title only."""
        mock_client = MagicMock()
        mock_client.embed.return_value = [0.5, 0.5, 0.5, 0.5]
        mock_get_client.return_value = mock_client

        score = self.strategy.score(
            alert_title="CPU overload detected",
            alert_service="",
            incident_title="CPU overload detected",
            incident_services=[],
        )
        assert score == pytest.approx(0.7)


class TestCosineSimilarity:
    """Unit tests for cosine similarity calculation."""

    def test_identical_vectors(self):
        """Identical vectors have cosine similarity of 1.0."""
        sim = SimilarityStrategy._cosine_similarity([1, 2, 3], [1, 2, 3])
        assert sim == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors have cosine similarity of 0.0."""
        sim = SimilarityStrategy._cosine_similarity([1, 0], [0, 1])
        assert sim == pytest.approx(0.0)

    def test_opposite_vectors(self):
        """Opposite vectors have cosine similarity of 0.0 (clamped)."""
        sim = SimilarityStrategy._cosine_similarity([1, 0], [-1, 0])
        assert sim == 0.0

    def test_empty_vectors(self):
        """Empty vectors return 0.0."""
        sim = SimilarityStrategy._cosine_similarity([], [])
        assert sim == 0.0

    def test_mismatched_lengths(self):
        """Mismatched vector lengths return 0.0."""
        sim = SimilarityStrategy._cosine_similarity([1, 2], [1, 2, 3])
        assert sim == 0.0


class TestJaccardFallback:
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
