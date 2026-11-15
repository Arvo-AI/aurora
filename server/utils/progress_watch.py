"""Progress tracking for a running investigation.

The heartbeat in ``chat.background.liveness`` proves the worker process is
alive. It cannot tell a long command from a call that will never return,
because both look the same from outside: a live process with nothing new to
show. This module is the other half: the code doing the work reports progress,
and the heartbeat thread reads it to decide whether the investigation is stuck.

Two kinds of report:

* ``note_progress()``: something happened (a model chunk, a tool call started
  or ended). Waiting longer than the idle window after the last one is a stall.
* ``bounded_call(seconds, label)``: the caller is inside a call that enforces
  its own limit (a command with a timeout, a sub-agent under ``wait_for``).
  While it runs, the stall deadline moves out to that limit plus a grace, so a
  20-minute query with a 20-minute timeout is never mistaken for a hang.

Both are no-ops outside a watched investigation (web chats, scripts, tests),
so callers never need to check.
"""

from __future__ import annotations

import contextvars
import itertools
import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional, Tuple

# A bounded call overrunning its own limit by this much is stuck, not slow:
# its timeout should already have ended it.
BOUND_GRACE_SECONDS = 120

_current_tracker: contextvars.ContextVar[Optional["ProgressTracker"]] = contextvars.ContextVar(
    "_current_progress_tracker", default=None,
)


class ProgressTracker:
    """Last progress time and the deadlines of the bounded calls in flight.

    Written from the event loop and from tool threads, read from the heartbeat
    thread, so every access takes the lock.
    """

    def __init__(self, idle_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_progress = clock()
        self._bounds: Dict[int, Tuple[float, float, str]] = {}  # id -> (started, deadline, label)
        self._ids = itertools.count()

    def progress(self) -> None:
        now = self._clock()
        with self._lock:
            self._last_progress = now

    @contextmanager
    def bounded(self, seconds: float, label: str) -> Iterator[None]:
        started = self._clock()
        bound_id = next(self._ids)
        with self._lock:
            self._last_progress = started
            self._bounds[bound_id] = (started, started + seconds + BOUND_GRACE_SECONDS, label)
        try:
            yield
        finally:
            ended = self._clock()
            with self._lock:
                self._bounds.pop(bound_id, None)
                self._last_progress = ended

    def stall_reason(self) -> Optional[str]:
        """Why the investigation counts as stuck now, or None while it is not.

        Stuck means past every deadline at once: the idle window since the last
        progress, and the limit of each bounded call still running. Parallel
        sub-agents each hold their own bound, so the longest one sets the pace.
        """
        if self.idle_seconds <= 0:
            return None
        now = self._clock()
        with self._lock:
            idle_deadline = self._last_progress + self.idle_seconds
            bounds = list(self._bounds.values())
            last_progress = self._last_progress
        latest_bound = max(bounds, key=lambda b: b[1]) if bounds else None
        if now <= idle_deadline or (latest_bound and now <= latest_bound[1]):
            return None
        if latest_bound:
            started, deadline, label = latest_bound
            limit = deadline - started - BOUND_GRACE_SECONDS
            return f"{label} ran {int(now - started)}s, past its {int(limit)}s limit"
        return f"no progress for {int(now - last_progress)}s"


def current_tracker() -> Optional[ProgressTracker]:
    return _current_tracker.get()


def set_tracker(tracker: Optional[ProgressTracker]) -> contextvars.Token:
    return _current_tracker.set(tracker)


def reset_tracker(token: contextvars.Token) -> None:
    _current_tracker.reset(token)


def note_progress() -> None:
    tracker = _current_tracker.get()
    if tracker is not None:
        tracker.progress()


@contextmanager
def bounded_call(seconds: Optional[float], label: str) -> Iterator[None]:
    """Mark a call that enforces its own *seconds* limit. No-op when unwatched."""
    tracker = _current_tracker.get()
    if tracker is None or not seconds or seconds <= 0:
        yield
        return
    with tracker.bounded(seconds, label):
        yield
