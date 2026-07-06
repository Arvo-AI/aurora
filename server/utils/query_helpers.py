"""Small, generic helpers for turning SQL query results into JSON payloads.

These were previously re-implemented in nearly every route and agent tool that
builds an API response from a cursor: cursor → list-of-dicts, UTC ISO-8601
formatting (the duplicated ``_format_timestamp`` / ``_iso``), duration math, and
value clamping. Centralized here so callers import instead of copy-paste.
"""

from datetime import timezone
from typing import Optional


def fetch_dicts(cursor) -> list[dict]:
    """Fetch all remaining rows as dicts keyed by column name."""
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def iso_utc(ts) -> Optional[str]:
    """ISO-8601 string for a datetime, treating naive values as UTC.

    PostgreSQL ``TIMESTAMP`` columns come back naive; tag them UTC so clients
    don't misread them as local time.
    """
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def duration_ms(start, end) -> Optional[int]:
    """Elapsed milliseconds between two datetimes, or None if either is missing."""
    if not (start and end):
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def clamp(value, low: int, high: int) -> int:
    """Clamp an int-coercible value into the inclusive ``[low, high]`` range."""
    return max(low, min(int(value), high))
