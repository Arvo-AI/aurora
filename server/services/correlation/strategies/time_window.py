"""
Time-window correlation strategy.

Scores an alert against an incident based on temporal proximity.
Uses linear decay within a configurable time window.
"""

from datetime import datetime

from services.correlation.strategies.base import CorrelationStrategy


class TimeWindowStrategy(CorrelationStrategy):
    """Linear-decay time proximity scoring.

    Args:
        time_window_seconds: Maximum gap (in seconds) for any correlation.
            Defaults to 300 (5 minutes).
    """

    def __init__(self, time_window_seconds: int = 300):
        self.time_window_seconds = time_window_seconds

    def score(
        self, alert_received_at: datetime, incident_updated_at: datetime
    ) -> float:
        """Score based on time gap between alert and incident.

        Args:
            alert_received_at: When the alert was received.
            incident_updated_at: When the incident was last updated.

        Returns:
            float: 1.0 when simultaneous, linearly decaying to 0.0 at the
            window boundary. Returns 0.0 if the alert precedes the incident
            update (negative gap) or exceeds the window.
        """
        gap_seconds = (alert_received_at - incident_updated_at).total_seconds()

        # Alert before incident update → no correlation
        if gap_seconds < 0:
            return 0.0

        if gap_seconds >= self.time_window_seconds:
            return 0.0

        return max(0.0, min(1.0, 1.0 - (gap_seconds / self.time_window_seconds)))
