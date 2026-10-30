"""Wall-clock limit for one investigation.

Celery stores this on the task at import, so a change needs a restart.
Out-of-range values are clamped so a bad setting still leaves a finite limit.
"""

import logging
import os

logger = logging.getLogger(__name__)

# One hour. The old 30-minute wall killed slower models mid-investigation.
DEFAULT_INVESTIGATION_TIMEOUT_SECONDS = 3600
# Soft limit needs a minute to mark the incident failed before the hard kill.
CLEANUP_GRACE_SECONDS = 60
# Below this the cleanup grace would eat the whole task.
MIN_INVESTIGATION_TIMEOUT_SECONDS = CLEANUP_GRACE_SECONDS * 2
# Matches Celery's app-level task_time_limit. Past this, one task pins a worker.
MAX_INVESTIGATION_TIMEOUT_SECONDS = 60 * 60 * 3

_ENV_VAR = "INVESTIGATION_TIMEOUT_SECONDS"


def investigation_timeout_seconds() -> int:
    """Hard limit in seconds. Unset, blank, or non-numeric uses the default."""
    raw = (os.getenv(_ENV_VAR) or "").strip()
    # Unset and blank both mean "ship the default", not "no limit".
    if not raw:
        return DEFAULT_INVESTIGATION_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        # A typo must not fall through to an unbounded task.
        logger.warning(
            "%s=%r is not an integer; using %ss",
            _ENV_VAR,
            raw,
            DEFAULT_INVESTIGATION_TIMEOUT_SECONDS,
        )
        return DEFAULT_INVESTIGATION_TIMEOUT_SECONDS
    # Shorter than the cleanup grace, the worker is killed before it can fail the incident.
    if value < MIN_INVESTIGATION_TIMEOUT_SECONDS:
        logger.warning(
            "%s=%s is below %ss; using %ss",
            _ENV_VAR,
            value,
            MIN_INVESTIGATION_TIMEOUT_SECONDS,
            MIN_INVESTIGATION_TIMEOUT_SECONDS,
        )
        return MIN_INVESTIGATION_TIMEOUT_SECONDS
    # Longer than three hours, one investigation holds a worker slot the rest of the day.
    if value > MAX_INVESTIGATION_TIMEOUT_SECONDS:
        logger.warning(
            "%s=%s is above %ss; using %ss",
            _ENV_VAR,
            value,
            MAX_INVESTIGATION_TIMEOUT_SECONDS,
            MAX_INVESTIGATION_TIMEOUT_SECONDS,
        )
        return MAX_INVESTIGATION_TIMEOUT_SECONDS
    return value


def investigation_soft_timeout_seconds() -> int:
    """When Celery raises, one minute before the hard kill."""
    return investigation_timeout_seconds() - CLEANUP_GRACE_SECONDS


def investigation_stale_after_seconds() -> int:
    # Five minutes past the hard kill. The soft-limit handler fails the run
    # first; this is only for a worker that died before it could.
    return investigation_timeout_seconds() + 5 * 60


def investigation_timeout_label() -> str:
    """Human duration for the failure card, e.g. '60 minutes'."""
    seconds = investigation_timeout_seconds()
    # Whole minutes match the Slack card. Anything else stays in seconds.
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{seconds} seconds"
