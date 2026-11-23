"""Checks that read a span of time instead of the present moment.

Most checks ask "what is true now" and cannot be asked about last Tuesday. A
check over logs or metrics can: its command names the span it reads with
placeholders for the start and the end. On a scheduled run they become the last
``window_seconds``. For a replay they become a span in the past, which is what
lets a check be tried against the incident it was written for.

The start and end can each be written three ways, because tools disagree on
how they take a time:

    {from}           {to}            RFC 3339, UTC   2026-11-10T14:00:00Z
    {from_epoch}     {to_epoch}      seconds         1794405600
    {from_epoch_ms}  {to_epoch_ms}   milliseconds    1794405600000

Only this module writes the values, as digits or one fixed timestamp format
with no characters a shell or a query language treats specially, so
substituting them cannot change what the command does.
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 7 * 24 * 3600
# Logs and metrics land late. A window may end this far before "now" at most.
MAX_LAG_SECONDS = 3600
_STAMP = "%Y-%m-%dT%H:%M:%SZ"

_FORMATS: Dict[str, Callable[[datetime], str]] = {
    "": lambda moment: moment.strftime(_STAMP),
    "_epoch": lambda moment: str(int(moment.timestamp())),
    "_epoch_ms": lambda moment: str(int(moment.timestamp()) * 1000),
}
_STARTS = tuple("{from%s}" % suffix for suffix in _FORMATS)
_ENDS = tuple("{to%s}" % suffix for suffix in _FORMATS)
FROM, TO = _STARTS[0], _ENDS[0]


def is_windowed(window_seconds: Optional[int]) -> bool:
    return bool(window_seconds)


def _names_start(command: str) -> bool:
    return any(placeholder in command for placeholder in _STARTS)


def _names_end(command: str) -> bool:
    return any(placeholder in command for placeholder in _ENDS)


def is_replayable(command: str, window_seconds: Optional[int]) -> bool:
    """Whether the command can be pointed at a past span.

    It needs both ends. A command with only a start reads from then until now,
    so asked about last week it would also see everything since.
    """
    return is_windowed(window_seconds) and _names_start(command) and _names_end(command)


def problem(command: str, window_seconds, interval_seconds: Optional[int] = None,
            lag_seconds=0) -> str:
    """Why this command, window and schedule cannot be used together, or ''."""
    has_placeholder = _names_start(command) or _names_end(command)
    if not window_seconds:
        if lag_seconds:
            return "lag_seconds only applies to a check with window_seconds"
        return "window_seconds is required when the command uses {from} or {to}" if has_placeholder else ""
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
        return "window_seconds must be an integer"
    if not MIN_WINDOW_SECONDS <= window_seconds <= MAX_WINDOW_SECONDS:
        return "window_seconds must be between %d and %d" % (MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS)
    if not _names_start(command):
        return "a windowed command must use {from} (or {from_epoch}, {from_epoch_ms}) for the start of the span it reads"
    if isinstance(lag_seconds, bool) or not isinstance(lag_seconds, int):
        return "lag_seconds must be an integer"
    if not 0 <= lag_seconds <= MAX_LAG_SECONDS:
        return "lag_seconds must be between 0 and %d" % MAX_LAG_SECONDS
    if interval_seconds and window_seconds < interval_seconds:
        # A 1-minute window run every 15 minutes never looks at the other 14.
        return ("window_seconds (%d) is shorter than interval_seconds (%d), so most of the time "
                "would never be read. Make the window at least as long as the interval."
                % (window_seconds, interval_seconds))
    return ""


def span_end(lag_seconds: Optional[int] = 0, now: Optional[datetime] = None) -> datetime:
    """Where a scheduled run's window ends: now, less the time the source needs to catch up."""
    return (now or datetime.now(timezone.utc)) - timedelta(seconds=lag_seconds or 0)


def render(command: str, window_seconds: Optional[int], end: Optional[datetime] = None,
           lag_seconds: Optional[int] = 0) -> str:
    """*command* with its span filled in.

    The span ends at *end* when one is given (a replay, or showing the span a
    past run read). Otherwise it ends now, less *lag_seconds*.
    """
    if not is_windowed(window_seconds):
        return command
    end = (end or span_end(lag_seconds)).astimezone(timezone.utc)
    start = end - timedelta(seconds=window_seconds)
    for suffix, write in _FORMATS.items():
        command = command.replace("{from%s}" % suffix, write(start)).replace("{to%s}" % suffix, write(end))
    return command
