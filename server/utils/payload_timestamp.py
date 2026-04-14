"""Best-effort extraction of alert fire timestamps from webhook payloads.

Different observability vendors use different field names and formats for the
"this is when the alert actually fired" timestamp. The SRE metrics dashboard
needs this populated on incidents to compute MTTD. This helper centralizes the
parsing so each provider task only declares which payload paths to try.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Coerce a value into a timezone-aware UTC datetime.

    Accepts ISO 8601 strings (with or without ``Z`` suffix), Unix timestamps as
    int/float (treated as milliseconds when > 1e12, otherwise seconds), and
    datetime objects (naive datetimes are tagged UTC). Returns ``None`` for
    anything we can't recognize.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if isinstance(value, bool):
        # bool is a subclass of int — exclude it explicitly to avoid surprises
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def extract_alert_fired_at(
    payload: Any, field_paths: Iterable[str]
) -> Optional[datetime]:
    """Try each dot-separated path in order, return the first parseable datetime.

    Path components may be dict keys or numeric list indices. Example::

        extract_alert_fired_at(payload, [
            "startsAt",
            "alerts.0.startsAt",
            "incident.created_at",
        ])
    """
    for path in field_paths:
        value = _walk(payload, path)
        parsed = parse_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _walk(obj: Any, path: str) -> Any:
    """Walk a dot-separated path through nested dicts/lists; ``None`` on miss."""
    current: Any = obj
    for part in path.split("."):
        if current is None:
            return None
        if part.isdigit() and isinstance(current, list):
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current
