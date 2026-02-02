"""
Alert-to-incident correlation orchestrator.

Combines topology, time-window and similarity strategies with weighted
scoring to decide whether an incoming alert should be attached to an
existing open incident.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.correlation.strategies import (
    SimilarityStrategy,
    TimeWindowStrategy,
    TopologyStrategy,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CorrelationResult:
    """Outcome of an alert-to-incident correlation attempt."""

    is_correlated: bool
    incident_id: Optional[str] = None
    score: float = 0.0
    strategy: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AlertCorrelator:
    """Weighted multi-strategy correlator with shadow-mode support.

    Environment variables (all have sensible defaults):
        CORRELATION_ENABLED          – master kill-switch (default ``true``)
        CORRELATION_SHADOW_MODE      – log but never attach (default ``false``)
        CORRELATION_TIME_WINDOW_SECONDS – lookback for candidate incidents (300)
        CORRELATION_SCORE_THRESHOLD  – minimum weighted score to attach (0.6)
        CORRELATION_TOPOLOGY_WEIGHT  – weight for topology strategy (0.5)
        CORRELATION_TIME_WEIGHT      – weight for time-window strategy (0.3)
        CORRELATION_SIMILARITY_WEIGHT – weight for similarity strategy (0.2)
        CORRELATION_MAX_GROUP_SIZE   – max alerts per incident (50)
    """

    _NOT_CORRELATED = CorrelationResult(is_correlated=False)

    def __init__(self) -> None:
        self.enabled = os.getenv("CORRELATION_ENABLED", "true").lower() == "true"
        self.shadow_mode = (
            os.getenv("CORRELATION_SHADOW_MODE", "false").lower() == "true"
        )
        self.time_window_seconds = int(
            os.getenv("CORRELATION_TIME_WINDOW_SECONDS", "300")
        )
        self.score_threshold = float(os.getenv("CORRELATION_SCORE_THRESHOLD", "0.6"))
        self.topology_weight = float(os.getenv("CORRELATION_TOPOLOGY_WEIGHT", "0.5"))
        self.time_weight = float(os.getenv("CORRELATION_TIME_WEIGHT", "0.3"))
        self.similarity_weight = float(
            os.getenv("CORRELATION_SIMILARITY_WEIGHT", "0.2")
        )
        self.max_group_size = int(os.getenv("CORRELATION_MAX_GROUP_SIZE", "50"))

        # Instantiate strategies
        self._topology = TopologyStrategy()
        self._time_window = TimeWindowStrategy(
            time_window_seconds=self.time_window_seconds
        )
        self._similarity = SimilarityStrategy()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def correlate(
        self,
        cursor,
        user_id: str,
        alert_title: str,
        alert_service: str,
        alert_severity: str,
        alert_received_at: datetime,
    ) -> CorrelationResult:
        """Attempt to correlate an alert with an existing open incident.

        The caller is responsible for providing an active database *cursor*
        (inside an open transaction).  This method never opens new DB
        connections.

        Args:
            cursor: An open ``psycopg2`` cursor.
            user_id: Owner / tenant identifier.
            alert_title: Title or summary of the incoming alert.
            alert_service: Service name from the alert.
            alert_severity: Severity label (e.g. ``critical``).
            alert_received_at: Timestamp when the alert was received.

        Returns:
            CorrelationResult: describes whether (and how) the alert matched.
        """
        try:
            if not self.enabled:
                logger.debug("[CORRELATION] Disabled via CORRELATION_ENABLED")
                return self._NOT_CORRELATED

            candidates = self._get_candidate_incidents(
                cursor,
                user_id,
                alert_received_at,
            )
            if not candidates:
                logger.debug("[CORRELATION] No candidate incidents found")
                return self._NOT_CORRELATED

            best_result: Optional[CorrelationResult] = None

            for candidate in candidates:
                result = self._score_candidate(
                    candidate=candidate,
                    user_id=user_id,
                    alert_title=alert_title,
                    alert_service=alert_service,
                    alert_received_at=alert_received_at,
                )
                if best_result is None or result.score > best_result.score:
                    best_result = result

            if best_result is None or best_result.score < self.score_threshold:
                logger.debug(
                    "[CORRELATION] Best score %.3f below threshold %.3f",
                    best_result.score if best_result else 0.0,
                    self.score_threshold,
                )
                return self._NOT_CORRELATED

            # Shadow mode: log the decision but report not-correlated
            if self.shadow_mode:
                logger.info(
                    "[CORRELATION][SHADOW] Would correlate alert '%s' to incident %s "
                    "(score=%.3f, strategy=%s)",
                    alert_title,
                    best_result.incident_id,
                    best_result.score,
                    best_result.strategy,
                )
                return self._NOT_CORRELATED

            # Max group-size guard
            incident_id = best_result.incident_id
            cursor.execute(
                "SELECT COUNT(*) FROM incident_alerts WHERE incident_id = %s",
                (incident_id,),
            )
            row = cursor.fetchone()
            group_count = row[0] if row else 0
            if group_count >= self.max_group_size:
                logger.warning(
                    "[CORRELATION] Incident %s has %d alerts (max %d), skipping",
                    incident_id,
                    group_count,
                    self.max_group_size,
                )
                return self._NOT_CORRELATED

            logger.info(
                "[CORRELATION] Correlated alert '%s' to incident %s "
                "(score=%.3f, strategy=%s)",
                alert_title,
                incident_id,
                best_result.score,
                best_result.strategy,
            )
            return best_result

        except Exception:
            logger.exception("[CORRELATION] Unexpected error during correlation")
            return self._NOT_CORRELATED

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_candidate_incidents(
        self,
        cursor,
        user_id: str,
        alert_received_at: datetime,
    ) -> List[Dict[str, Any]]:
        """Fetch open incidents updated within the time window.

        Returns at most 20 rows ordered by most-recently updated first.
        """
        cutoff = alert_received_at - timedelta(seconds=self.time_window_seconds)

        cursor.execute(
            """
            SELECT id, alert_title, alert_service, updated_at
            FROM incidents
            WHERE user_id = %s
              AND status = 'investigating'
              AND updated_at >= %s
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (user_id, cutoff),
        )
        rows = cursor.fetchall()
        return [
            {
                "id": str(row[0]),
                "alert_title": row[1] or "",
                "alert_service": row[2] or "",
                "updated_at": row[3],
            }
            for row in rows
        ]

    def _score_candidate(
        self,
        candidate: Dict[str, Any],
        user_id: str,
        alert_title: str,
        alert_service: str,
        alert_received_at: datetime,
    ) -> CorrelationResult:
        """Compute weighted score for a single candidate incident."""

        incident_id = candidate["id"]
        incident_title = candidate["alert_title"]
        incident_service = candidate["alert_service"]
        incident_updated_at = candidate["updated_at"]

        incident_services = [incident_service] if incident_service else []

        scores: Dict[str, float] = {}

        # --- Topology ---
        try:
            scores["topology"] = self._topology.score(
                alert_service,
                incident_services,
                user_id,
            )
        except Exception:
            logger.warning("[CORRELATION] TopologyStrategy error", exc_info=True)
            scores["topology"] = 0.0

        # --- Time window ---
        try:
            scores["time_window"] = self._time_window.score(
                alert_received_at,
                incident_updated_at,
            )
        except Exception:
            logger.warning("[CORRELATION] TimeWindowStrategy error", exc_info=True)
            scores["time_window"] = 0.0

        # --- Similarity ---
        try:
            scores["similarity"] = self._similarity.score(
                alert_title,
                alert_service,
                incident_title,
                incident_services,
            )
        except Exception:
            logger.warning("[CORRELATION] SimilarityStrategy error", exc_info=True)
            scores["similarity"] = 0.0

        weighted = (
            self.topology_weight * scores["topology"]
            + self.time_weight * scores["time_window"]
            + self.similarity_weight * scores["similarity"]
        )

        # Identify the dominant strategy
        dominant = max(scores, key=scores.get)  # type: ignore[arg-type]

        return CorrelationResult(
            is_correlated=True,
            incident_id=incident_id,
            score=weighted,
            strategy=dominant,
            details=scores,
        )
