"""Tests for TopologyStrategy."""

from unittest.mock import patch, MagicMock

import pytest

from services.correlation.strategies.topology import TopologyStrategy


class TestTopologyStrategy:
    """Suite for graph-based topology scoring."""

    def setup_method(self):
        self.strategy = TopologyStrategy()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mock_client(self, upstream=None, downstream=None):
        """Return a mock MemgraphClient with configured traversal results."""
        client = MagicMock(name="MemgraphClient")
        client.get_all_upstream.return_value = upstream or []
        client.get_all_downstream.return_value = downstream or []
        return client

    # ------------------------------------------------------------------
    # Direct / upstream connections
    # ------------------------------------------------------------------

    def test_direct_upstream_returns_one(self):
        """1-hop upstream (direct dependency) → 1.0."""
        client = self._mock_client(
            upstream=[{"name": "db-primary", "depth": 1}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["db-primary"], "user-1")
        assert score == pytest.approx(1.0)

    def test_two_hop_upstream_returns_0_7(self):
        """2-hop upstream → 0.7."""
        client = self._mock_client(
            upstream=[{"name": "cache-redis", "depth": 2}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["cache-redis"], "user-1")
        assert score == pytest.approx(0.7)

    def test_three_hop_upstream_returns_0_4(self):
        """3-hop upstream → 0.4."""
        client = self._mock_client(
            upstream=[{"name": "auth-service", "depth": 3}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["auth-service"], "user-1")
        assert score == pytest.approx(0.4)

    # ------------------------------------------------------------------
    # Downstream connections
    # ------------------------------------------------------------------

    def test_one_hop_downstream_returns_0_8(self):
        """1-hop downstream → 0.8."""
        client = self._mock_client(
            downstream=[{"name": "web-frontend", "depth": 1}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["web-frontend"], "user-1")
        assert score == pytest.approx(0.8)

    def test_two_hop_downstream_returns_0_5(self):
        """2-hop downstream → 0.5."""
        client = self._mock_client(
            downstream=[{"name": "cdn-layer", "depth": 2}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["cdn-layer"], "user-1")
        assert score == pytest.approx(0.5)

    def test_three_hop_downstream_returns_0_2(self):
        """3-hop downstream → 0.2."""
        client = self._mock_client(
            downstream=[{"name": "analytics", "depth": 3}],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["analytics"], "user-1")
        assert score == pytest.approx(0.2)

    # ------------------------------------------------------------------
    # Max-score across multiple incident services
    # ------------------------------------------------------------------

    def test_max_score_across_incident_services(self):
        """Return the best score when multiple incident services exist."""
        client = self._mock_client(
            upstream=[
                {"name": "svc-a", "depth": 3},  # 0.4
                {"name": "svc-b", "depth": 1},  # 1.0
            ],
        )
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["svc-a", "svc-b"], "user-1")
        assert score == pytest.approx(1.0)

    # ------------------------------------------------------------------
    # Disconnected / error handling
    # ------------------------------------------------------------------

    def test_disconnected_services_returns_zero(self):
        """Services not in the graph → 0.0."""
        client = self._mock_client()  # empty results
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            return_value=client,
        ):
            score = self.strategy.score("api-server", ["unknown-svc"], "user-1")
        assert score == 0.0

    def test_memgraph_exception_returns_zero(self):
        """Any Memgraph failure → 0.0 (graceful degradation)."""
        with patch(
            "services.graph.memgraph_client.get_memgraph_client",
            side_effect=RuntimeError("connection refused"),
        ):
            score = self.strategy.score("api-server", ["db-primary"], "user-1")
        assert score == 0.0

    def test_empty_alert_service_returns_zero(self):
        """Empty alert_service string → 0.0."""
        assert self.strategy.score("", ["db-primary"], "user-1") == 0.0

    def test_empty_incident_services_returns_zero(self):
        """Empty incident_services list → 0.0."""
        assert self.strategy.score("api-server", [], "user-1") == 0.0
