"""Wall-clock limit for one investigation.

Celery stores this on the task at import, so a change needs a restart.
Out-of-range values are clamped so a bad setting still leaves a finite limit.
"""

import logging
import os
from typing import Optional

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


# Ten minutes with no progress and no bounded call still inside its own limit.
# Model calls are bounded calls (INVESTIGATION_MODEL_CALL_SECONDS), so a model
# that thinks for a long time before its first token is not counted as idle.
DEFAULT_INVESTIGATION_STALL_SECONDS = 600
MIN_INVESTIGATION_STALL_SECONDS = 120
_STALL_ENV_VAR = "INVESTIGATION_STALL_SECONDS"

# How long one model call may run before it counts as stuck. Thinking models
# can take many minutes on a large investigation context; a call that has not
# returned after half an hour is hung, not thinking.
DEFAULT_MODEL_CALL_SECONDS = 1800
MIN_MODEL_CALL_SECONDS = 120
_MODEL_CALL_ENV_VAR = "INVESTIGATION_MODEL_CALL_SECONDS"

# How long before the time limit the agent is told to stop and conclude, so
# the run ends with a report instead of being cut off.
DEFAULT_WRAP_UP_SECONDS = 300
MIN_WRAP_UP_SECONDS = 60
_WRAP_UP_ENV_VAR = "INVESTIGATION_WRAPUP_SECONDS"


def _seconds_setting(env_var: str, default: int, minimum: int) -> int:
    """Seconds from *env_var*: unset or invalid uses *default*, 0 or less turns it off."""
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %ss", env_var, raw, default)
        return default
    if value <= 0:
        return 0
    if value < minimum:
        logger.warning("%s=%s is below %ss; using %ss", env_var, value, minimum, minimum)
        return minimum
    return value


def investigation_stall_seconds() -> int:
    """How long a watched investigation may go without progress. 0 turns the check off."""
    # A shorter window cancels healthy runs between two slow tool calls.
    return _seconds_setting(
        _STALL_ENV_VAR, DEFAULT_INVESTIGATION_STALL_SECONDS, MIN_INVESTIGATION_STALL_SECONDS,
    )


def investigation_model_call_seconds() -> int:
    """How long one model call may run before the stall check counts it as stuck.

    0 removes the allowance: a model call then counts against the stall window
    like any other silence.
    """
    return _seconds_setting(_MODEL_CALL_ENV_VAR, DEFAULT_MODEL_CALL_SECONDS, MIN_MODEL_CALL_SECONDS)


def investigation_wrap_up_after_seconds() -> Optional[int]:
    """Seconds after the start at which the agent is told to conclude, or None.

    The notice lands INVESTIGATION_WRAPUP_SECONDS before the soft limit (which
    itself fires CLEANUP_GRACE_SECONDS before the hard kill). 0 turns it off; a
    budget too short to leave a minute of investigation also turns it off.
    """
    wrap_up = _seconds_setting(_WRAP_UP_ENV_VAR, DEFAULT_WRAP_UP_SECONDS, MIN_WRAP_UP_SECONDS)
    if wrap_up <= 0:
        return None
    after = investigation_soft_timeout_seconds() - wrap_up
    return after if after >= 60 else None


def investigation_timeout_label() -> str:
    """Human duration for the failure card, e.g. '60 minutes'."""
    seconds = investigation_timeout_seconds()
    # Whole minutes match the Slack card. Anything else stays in seconds.
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    return f"{seconds} seconds"
