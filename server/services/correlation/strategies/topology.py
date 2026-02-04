"""
Topology correlation strategy.

Scores an alert against an incident by checking the dependency graph
distance between the alert's service and the incident's services.
"""

import logging
from typing import List

from services.correlation.strategies.base import CorrelationStrategy

logger = logging.getLogger(__name__)

# Upstream scores (alert depends on incident service):
#   1-hop: direct dependency -> high correlation (1.0)
#   2-hop: indirect dependency -> moderate correlation (0.7)
#   3-hop: distant dependency -> low correlation (0.4)
_UPSTREAM_SCORES = {1: 1.0, 2: 0.7, 3: 0.4}

# Downstream scores (incident service depends on alert service):
#   1-hop: direct dependent -> moderate correlation (0.8)
#   2-hop: indirect dependent -> low correlation (0.5)
#   3-hop: distant dependent -> very low correlation (0.2)
_DOWNSTREAM_SCORES = {1: 0.8, 2: 0.5, 3: 0.2}


class TopologyStrategy(CorrelationStrategy):
    """Graph-distance scoring using the Memgraph dependency topology."""

    def score(
        self,
        alert_service: str,
        incident_services: List[str],
        user_id: str,
    ) -> float:
        """Score based on graph distance between alert and incident services.

        For each incident service the strategy looks up upstream and downstream
        paths from the alert service and assigns a score based on hop depth.

        Args:
            alert_service: Name of the service that fired the alert.
            incident_services: Service names already associated with the incident.
            user_id: Owner of the topology graph.

        Returns:
            float: Maximum score across all incident services. 0.0 on any error
            or when services are disconnected.
        """
        if not alert_service or not incident_services:
            logger.warning(
                "[TopologyStrategy] Empty input - alert_service=%r, incident_services=%r",
                bool(alert_service),
                bool(incident_services),
            )
            return 0.0

        try:
            from services.graph.memgraph_client import get_memgraph_client

            client = get_memgraph_client()

            # Fetch upstream/downstream neighbours of the alert service once
            upstream_list = client.get_all_upstream(user_id, alert_service, max_depth=3)
            downstream_list = client.get_all_downstream(
                user_id, alert_service, max_depth=3
            )

            # Build {service_name: depth} maps
            upstream_map = {
                entry["name"]: entry["depth"]
                for entry in upstream_list
                if "name" in entry and "depth" in entry
            }
            downstream_map = {
                entry["name"]: entry["depth"]
                for entry in downstream_list
                if "name" in entry and "depth" in entry
            }

            best_score = 0.0
            for svc in incident_services:
                # Exact match: same service -> perfect correlation
                if svc == alert_service:
                    return 1.0
                
                # Upstream check (alert depends on incident service)
                if svc in upstream_map:
                    depth = upstream_map[svc]
                    best_score = max(best_score, _UPSTREAM_SCORES.get(depth, 0.0))

                # Downstream check (incident service depends on alert service)
                if svc in downstream_map:
                    depth = downstream_map[svc]
                    best_score = max(best_score, _DOWNSTREAM_SCORES.get(depth, 0.0))

            return best_score

        except Exception:
            logger.warning(
                "TopologyStrategy: failed to score alert_service=%s, returning 0.0",
                alert_service,
                exc_info=True,
            )
            return 0.0
