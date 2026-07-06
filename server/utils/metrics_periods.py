"""Shared metric period helpers.

A single source of truth for the ``7d/30d/90d/...`` period strings and their
PostgreSQL interval equivalents, used by the SRE metrics routes and the
internal agent's introspection tools so the two never drift apart.
"""

# Accepted period values mapped to their PostgreSQL interval literal.
PERIOD_MAP = {
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
    "180d": "180 days",
    "365d": "365 days",
}

DEFAULT_INTERVAL = "30 days"


def period_to_interval(period: str) -> str:
    """Return the PostgreSQL interval literal for a period, defaulting to 30 days."""
    return PERIOD_MAP.get(period, DEFAULT_INTERVAL)
