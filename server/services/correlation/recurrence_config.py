"""Configuration for root-cause recurrence detection (dedup layer 1).

Deliberately free of heavy imports: this module is imported by every provider
webhook task, so it must stay cheap (stdlib only).
"""

import logging
import os

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"

_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)

# How long a recurrence group stays fold-eligible: if nothing in the group
# (anchor or any folded child) fired within this window, the group is stale
# and a new standalone incident is created instead.
GROUP_IDLE_HOURS = 24

# Max agent loop turns for one recurrence check (economy bound; the wall-clock
# timeout below is the primary bound).
MAX_TURNS = 15

# Reject reasons persisted on recurrence_verdicts.reject_reason (VARCHAR(30)).
REJECT_INVALID_ID = "invalid_id"
REJECT_SELF_REFERENCE = "self_reference"
REJECT_MERGED_TARGET = "merged_target"
REJECT_MERGED_CHILD = "merged_child"
REJECT_STALE_GROUP = "stale_group"
REJECT_ALREADY_FOLDED = "already_folded"
REJECT_MUTUAL_FOLD_LOST = "mutual_fold_lost"
REJECT_CONTENTION = "contention"
REJECT_TIMEOUT = "timeout"
REJECT_NO_VERDICT = "no_verdict"
REJECT_ERROR = "error"


def get_recurrence_mode() -> str:
    """Read RECURRENCE_DETECTION_MODE per call, clamped to off|shadow|live.

    Unknown values warn and degrade to 'off' so a typo in a values file can
    never change behavior or spend tokens.
    """
    raw = (os.getenv("RECURRENCE_DETECTION_MODE") or MODE_OFF).strip().lower()
    if raw not in _VALID_MODES:
        logger.warning(
            "[RECURRENCE] Unknown RECURRENCE_DETECTION_MODE=%r; treating as 'off'",
            raw,
        )
        return MODE_OFF
    return raw


def is_live() -> bool:
    return get_recurrence_mode() == MODE_LIVE


def get_agent_timeout_seconds() -> int:
    """Wall-clock budget for one recurrence check.

    Must stay well under the summarization task's soft_time_limit minus the
    summary-generation work (see chat/background/summarization.py).
    """
    raw = os.getenv("RECURRENCE_AGENT_TIMEOUT_SECONDS") or "120"
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning(
            "[RECURRENCE] Invalid RECURRENCE_AGENT_TIMEOUT_SECONDS=%r; using 120", raw
        )
        return 120
    if value <= 0:
        # 0/negative would cancel every check at its first await while still
        # paying agent setup per incident — treat as misconfiguration.
        logger.warning(
            "[RECURRENCE] Non-positive RECURRENCE_AGENT_TIMEOUT_SECONDS=%r; using 120",
            raw,
        )
        return 120
    return value
