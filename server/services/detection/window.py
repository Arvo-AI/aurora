"""Checks that read a span of time instead of the present moment.

Most checks ask "what is true now" and cannot be asked about last Tuesday. A
check over logs or metrics can: its command names the span it reads with the
placeholders ``{from}`` and ``{to}``. On a scheduled run they become the last
``window_seconds``. For a replay they become a span in the past, which is what
lets a check be tried against the incident it was written for.

Only this module writes the timestamps, in one fixed format with no characters
a shell or a query language treats specially, so substituting them cannot
change what the command does.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

FROM, TO = "{from}", "{to}"
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 7 * 24 * 3600
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def is_windowed(window_seconds: Optional[int]) -> bool:
    return bool(window_seconds)


def is_replayable(command: str, window_seconds: Optional[int]) -> bool:
    """Whether the command can be pointed at a past span.

    It needs both ends. A command with only ``{from}`` reads from then until
    now, so asked about last week it would also see everything since.
    """
    return is_windowed(window_seconds) and FROM in command and TO in command


def problem(command: str, window_seconds) -> str:
    """Why this command and window cannot be used together, or ''."""
    has_placeholder = FROM in command or TO in command
    if not window_seconds:
        return "window_seconds is required when the command uses {from} or {to}" if has_placeholder else ""
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
        return "window_seconds must be an integer"
    if not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS:
        return "window_seconds must be between %d and %d" % (MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS)
    if FROM not in command:
        return "a windowed command must use {from} for the start of the span it reads"
    return ""


def render(command: str, window_seconds: Optional[int], end: Optional[datetime] = None) -> str:
    """*command* with its span filled in, ending at *end* (default: now)."""
    if not is_windowed(window_seconds):
        return command
    end = (end or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(seconds=window_seconds)
    return command.replace(FROM, start.strftime(_STAMP)).replace(TO, end.strftime(_STAMP))
