"""Liveness heartbeat and stall watchdog for a background chat task.

Two questions decide whether a running investigation should be stopped, and
they need different signals:

* Is the worker process alive? A thread in the worker writes the session's
  ``heartbeat_at`` every 30 seconds for as long as the task runs, whatever the
  model, the tools or the event loop are doing. When the process dies the beats
  stop, and the cleanup sweep fails the session within a few minutes.
* Is the investigation still getting anywhere? The work reports progress
  (``utils.progress_watch``): model chunks, tool calls, and calls that enforce
  their own limit. On each beat the same thread checks it. Past the stall
  deadline it cancels the investigation, which then fails through the normal
  error path, so the user hears about it in minutes instead of at the
  investigation time limit.

If the cancel cannot land (a synchronous call is blocking the event loop), the
thread stops beating, so the sweep treats the task as dead, and asks Celery's
soft-time-limit handler to interrupt the blocked call so the worker slot frees.
"""

import asyncio
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator, Optional

from utils.progress_watch import ProgressTracker, reset_tracker, set_tracker

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
# How long a cancel gets to take effect before the watchdog escalates.
CANCEL_GRACE_SECONDS = 60
_LOG = "[BackgroundChat:Heartbeat]"


class InvestigationStalled(Exception):
    """The investigation made no progress within its stall deadline."""


def touch_session(session_id: str, user_id: str) -> bool:
    """Record that the worker running an in-progress session is alive.

    Writes ``heartbeat_at``, not ``updated_at``: a beat says nothing new about
    the conversation, so the chat history order stays put, and a session that
    is still queued (never beaten) stays distinguishable from a dead one.
    Never touches a finished session. Returns True when the row was touched.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # Without the RLS variables the UPDATE silently matches no row.
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                return False
            cursor.execute(
                "UPDATE chat_sessions SET heartbeat_at = NOW() WHERE id = %s AND status = 'in_progress'",
                (session_id,),
            )
            touched = cursor.rowcount == 1
        conn.commit()
    return touched


def _interrupt_main_thread() -> bool:
    """Raise Celery's soft time limit in this worker child, if it is installed.

    A blocked socket read or subprocess wait returns early on a signal, and the
    handler raises in the main thread. Without a handler the default action of
    SIGUSR1 would kill the process, so do nothing then.
    """
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False
    handler = signal.getsignal(sigusr1)
    if handler in (signal.SIG_DFL, signal.SIG_IGN, None):
        return False
    os.kill(os.getpid(), sigusr1)
    return True


class _Watchdog:
    """Cancels the investigation once its tracker says it is stuck."""

    def __init__(self, tracker: ProgressTracker, clock: Callable[[], float] = time.monotonic):
        self.tracker = tracker
        self._clock = clock
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task: Optional[asyncio.Task] = None
        self.stall_reason: Optional[str] = None
        self.cancelled_at: Optional[float] = None
        self.gave_up = False
        # Set by the main thread once asyncio.run has returned or raised. Past
        # that point a soft-time-limit signal would land outside run_watched's
        # error conversion and replace the run's real result, so the watchdog
        # checks it under the same lock before it signals.
        self._finish_lock = threading.Lock()
        self._finished = False

    def mark_finished(self) -> None:
        with self._finish_lock:
            self._finished = True

    def _interrupt_if_running(self) -> bool:
        with self._finish_lock:
            if self._finished:
                return False
            return _interrupt_main_thread()

    async def watch(self, coro: Awaitable[Any]) -> Any:
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        return await coro

    def check(self, stopping: threading.Event) -> None:
        if self.gave_up:
            return
        now = self._clock()
        if self.stall_reason is None:
            reason = self.tracker.stall_reason()
            if not reason:
                return
            self.stall_reason = reason
            self.cancelled_at = now
            logger.error("%s investigation stalled (%s); cancelling it", _LOG, reason)
            loop, task = self.loop, self.task
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    # The loop already closed: the run is finishing on its own.
                    pass
            return
        if now - self.cancelled_at < CANCEL_GRACE_SECONDS:
            return
        # The cancel is queued on a loop something synchronous is blocking.
        self.gave_up = True
        logger.error(
            "%s cancel did not take effect within %ss; no longer vouching for the worker",
            _LOG, CANCEL_GRACE_SECONDS,
        )
        if not stopping.is_set() and self._interrupt_if_running():
            logger.error("%s interrupted the blocked call through the soft time limit", _LOG)


def _beat_until_stopped(
    session_id: Optional[str],
    user_id: Optional[str],
    interval: float,
    stop: threading.Event,
    watchdog: Optional[_Watchdog],
) -> None:
    # The first beat is immediate: a task that waited in the queue starts with a
    # session that already looks stale.
    while True:
        if watchdog is not None:
            try:
                watchdog.check(stop)
            except Exception:
                logger.warning("%s stall check failed", _LOG, exc_info=True)
        # A watchdog that gave up stops vouching, so the sweep sees a dead worker.
        if session_id and user_id and not (watchdog is not None and watchdog.gave_up):
            try:
                touch_session(session_id, user_id)
            except Exception:
                # One missed beat is harmless; the sweep allows several.
                logger.warning("%s could not refresh session %s", _LOG, session_id, exc_info=True)
        if stop.wait(interval):
            return


@contextmanager
def session_heartbeat(
    session_id: Optional[str],
    user_id: Optional[str],
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    watchdog: Optional[_Watchdog] = None,
) -> Iterator[None]:
    """Beat (and watch, given a watchdog) for the duration of the ``with`` block.

    The caller's own error, timeout or cancellation all end the block, so the
    beats can never outlive the task they vouch for.
    """
    has_session = bool(session_id and user_id)
    if not has_session and watchdog is None:
        yield
        return

    stop = threading.Event()
    thread = threading.Thread(
        target=_beat_until_stopped,
        args=(
            str(session_id) if has_session else None,
            str(user_id) if has_session else None,
            interval, stop, watchdog,
        ),
        name=f"session-heartbeat-{str(session_id)[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # A beat caught mid-query finishes on its own; the thread is a daemon.
        thread.join(timeout=5)


def run_watched(
    coro: Awaitable[Any],
    session_id: Optional[str],
    user_id: Optional[str],
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
    idle_seconds: Optional[float] = None,
) -> Any:
    """``asyncio.run(coro)`` under the heartbeat and the stall watchdog.

    Raises :class:`InvestigationStalled` when the watchdog stopped the run, so
    the caller's normal error path fails the session and tells the user.
    """
    if idle_seconds is None:
        from chat.investigation_timeout import investigation_stall_seconds

        idle_seconds = investigation_stall_seconds()
    tracker = ProgressTracker(idle_seconds)
    watchdog = _Watchdog(tracker)
    # asyncio.run copies this context into the task, and tool threads copy it on.
    token = set_tracker(tracker)
    try:
        with session_heartbeat(session_id, user_id, interval=interval, watchdog=watchdog):
            try:
                try:
                    result = asyncio.run(watchdog.watch(coro))
                finally:
                    watchdog.mark_finished()
                return result
            except (Exception, asyncio.CancelledError) as exc:
                if watchdog.stall_reason and not isinstance(exc, InvestigationStalled):
                    raise InvestigationStalled(
                        f"Investigation stalled: {watchdog.stall_reason}"
                    ) from exc
                raise
    finally:
        reset_tracker(token)
