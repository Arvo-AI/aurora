"""
Alert-to-incident correlation orchestrator.

Combines topology, time-window and similarity strategies with weighted
scoring to decide whether an incoming alert should be attached to an
existing open incident.
"""

import json
import logging
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


@dataclass(frozen=True)
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
    """Weighted multi-strategy correlator with shadow-mode support."""

    _NOT_CORRELATED = CorrelationResult(is_correlated=False)

    ENABLED: bool = True
    SHADOW_MODE: bool = False
    TIME_WINDOW_SECONDS: int = 300
    SCORE_THRESHOLD: float = 0.6
    TOPOLOGY_WEIGHT: float = 0.5
    TIME_WEIGHT: float = 0.3
    SIMILARITY_WEIGHT: float = 0.2
    MAX_GROUP_SIZE: int = 50

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        shadow_mode: Optional[bool] = None,
        time_window_seconds: Optional[int] = None,
        score_threshold: Optional[float] = None,
        topology_weight: Optional[float] = None,
        time_weight: Optional[float] = None,
        similarity_weight: Optional[float] = None,
        max_group_size: Optional[int] = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else self.ENABLED
        self.shadow_mode = shadow_mode if shadow_mode is not None else self.SHADOW_MODE
        self.time_window_seconds = (
            time_window_seconds
            if time_window_seconds is not None
            else self.TIME_WINDOW_SECONDS
        )
        self.score_threshold = (
            score_threshold if score_threshold is not None else self.SCORE_THRESHOLD
        )
        self.topology_weight = (
            topology_weight if topology_weight is not None else self.TOPOLOGY_WEIGHT
        )
        self.time_weight = time_weight if time_weight is not None else self.TIME_WEIGHT
        self.similarity_weight = (
            similarity_weight
            if similarity_weight is not None
            else self.SIMILARITY_WEIGHT
        )
        self.max_group_size = (
            max_group_size if max_group_size is not None else self.MAX_GROUP_SIZE
        )

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
        source_type: str,
        source_alert_id: int,
        alert_title: str,
        alert_service: str,
        alert_severity: str,
        alert_metadata: Optional[Dict[str, Any]] = None,
    ) -> CorrelationResult:
        """Attempt to correlate an alert with an existing open incident.

        The caller is responsible for providing an active database *cursor*
        (inside an open transaction).  This method never opens new DB
        connections.

        Args:
            cursor: An open ``psycopg2`` cursor.
            user_id: Owner / tenant identifier.
            source_type: Alert source (e.g. ``grafana``).
            source_alert_id: Numeric alert ID from the source system.
            alert_title: Title or summary of the incoming alert.
            alert_service: Service name from the alert.
            alert_severity: Severity label (e.g. ``critical``).
            alert_metadata: Optional dict with extra alert data.  If it
                contains a ``received_at`` key the value is used as the
                alert timestamp (datetime or ISO-8601 string).

        Returns:
            CorrelationResult: describes whether (and how) the alert matched.
        """
        try:
            if not self.enabled:
                logger.debug("[CORRELATION] Disabled")
                return self._NOT_CORRELATED

            alert_received_at = self._resolve_received_at(alert_metadata)

            candidates = self._get_candidate_incidents(
                cursor,
                user_id,
                alert_received_at,
            )
            logger.info(
                "[CORRELATION] Found %d candidate incidents for user %s (alert_received_at=%s)",
                len(candidates),
                user_id,
                alert_received_at,
            )
            if not candidates:
                logger.info("[CORRELATION] No candidate incidents found")
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
                logger.info(
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

            # Max group-size guard (uses denormalised count from incidents row)
            incident_id = best_result.incident_id
            group_count = best_result.details.get("correlated_alert_count", 0)
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

    @staticmethod
    def _resolve_received_at(
        alert_metadata: Optional[Dict[str, Any]],
    ) -> datetime:
        """Extract or derive the alert received-at timestamp."""
        if alert_metadata and "received_at" in alert_metadata:
            val = alert_metadata["received_at"]
            if isinstance(val, datetime):
                return val
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    logger.warning(
                        "[CORRELATION] Invalid received_at timestamp: %r, using now()",
                        val,
                    )
        return datetime.now(timezone.utc)

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
            SELECT id, alert_title, alert_service, affected_services,
                   correlated_alert_count, started_at, updated_at
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
                "affected_services": row[3] if row[3] else [],
                "correlated_alert_count": row[4] if row[4] else 0,
                "started_at": row[5],
                "updated_at": row[6],
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
        incident_updated_at = candidate["updated_at"]

        # Prefer affected_services; fall back to single alert_service
        incident_services = candidate.get("affected_services") or []
        if not incident_services and candidate.get("alert_service"):
            incident_services = [candidate["alert_service"]]

        scores: Dict[str, float] = {}

        try:
            scores["topology"] = self._topology.score(
                alert_service,
                incident_services,
                user_id,
            )
        except Exception:
            logger.warning("[CORRELATION] TopologyStrategy error", exc_info=True)
            scores["topology"] = 0.0

        try:
            scores["time_window"] = self._time_window.score(
                alert_received_at,
                incident_updated_at,
            )
        except Exception:
            logger.warning("[CORRELATION] TimeWindowStrategy error", exc_info=True)
            scores["time_window"] = 0.0

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

        dominant = max(scores, key=scores.get)  # type: ignore[arg-type]

        return CorrelationResult(
            is_correlated=True,
            incident_id=incident_id,
            score=weighted,
            strategy=dominant,
            details={
                **scores,
                "correlated_alert_count": candidate.get("correlated_alert_count", 0),
            },
        )


# ---------------------------------------------------------------------------
# Unified correlated alert handler
# ---------------------------------------------------------------------------


def handle_correlated_alert(
    cursor,
    user_id: str,
    incident_id: str,
    source_type: str,
    source_alert_id: int,
    alert_title: str,
    alert_service: str,
    alert_severity: str,
    correlation_result: CorrelationResult,
    alert_metadata: Dict[str, Any],
    raw_payload: Dict[str, Any],
) -> None:
    """Handle a correlated alert: record it, update incident, notify SSE, and enqueue RCA context update.

    This is the unified function that all integrations should call when AlertCorrelator
    determines an alert is correlated to an existing incident.

    Args:
        cursor: An open psycopg2 cursor (inside a transaction).
        user_id: Owner / tenant identifier.
        incident_id: The ID of the incident this alert correlates to.
        source_type: Alert source (e.g. 'grafana', 'pagerduty', 'datadog').
        source_alert_id: Numeric alert ID from the source-specific table.
        alert_title: Title or summary of the incoming alert.
        alert_service: Service name from the alert.
        alert_severity: Severity label (e.g. 'critical').
        correlation_result: The CorrelationResult from AlertCorrelator.correlate().
        alert_metadata: Dict with source-specific metadata.
        raw_payload: The complete raw webhook payload for context injection.
    """
    # 1. Insert into incident_alerts
    cursor.execute(
        """INSERT INTO incident_alerts
           (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
            alert_severity, correlation_strategy, correlation_score,
            correlation_details, alert_metadata)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            user_id,
            incident_id,
            source_type,
            source_alert_id,
            alert_title,
            alert_service,
            alert_severity,
            correlation_result.strategy,
            correlation_result.score,
            json.dumps(correlation_result.details),
            json.dumps(alert_metadata),
        ),
    )

    # 2. Update incident (increment count, add service to affected_services)
    cursor.execute(
        """UPDATE incidents
           SET correlated_alert_count = correlated_alert_count + 1,
               affected_services = CASE
                   WHEN NOT (%s = ANY(affected_services)) THEN array_append(affected_services, %s)
                   ELSE affected_services
               END,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = %s""",
        (alert_service, alert_service, incident_id),
    )

    # 3. Broadcast SSE notification
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections

        broadcast_incident_update_to_user_connections(
            user_id,
            {
                "type": "alert_correlated",
                "incident_id": str(incident_id),
                "source": source_type,
                "alert_title": alert_title,
                "correlation_score": correlation_result.score,
            },
        )
    except Exception as e:
        logger.warning("[CORRELATION] Failed to notify SSE: %s", e)

    # 4. Enqueue RCA context update if there's an active RCA session
    try:
        from chat.background.context_updates import enqueue_rca_context_update

        cursor.execute(
            """
            SELECT aurora_chat_session_id
            FROM incidents
            WHERE id = %s AND user_id = %s
            """,
            (incident_id, user_id),
        )
        row = cursor.fetchone()
        if row and row[0]:
            session_id = str(row[0])
            enqueue_rca_context_update(
                user_id=user_id,
                session_id=session_id,
                source=source_type,
                payload=raw_payload,
                incident_id=str(incident_id),
                event_id=str(source_alert_id) if source_alert_id else None,
            )
            logger.info(
                "[CORRELATION][RCA-UPDATE] Enqueued context update for correlated %s alert to session %s (incident=%s)",
                source_type,
                session_id,
                incident_id,
            )
    except Exception as e:
        logger.warning("[CORRELATION][RCA-UPDATE] Failed to enqueue context update: %s", e)

    logger.info(
        "[CORRELATION] Alert correlated to incident %s (source=%s, score=%.2f, strategy=%s)",
        incident_id,
        source_type,
        correlation_result.score,
        correlation_result.strategy,
    )
