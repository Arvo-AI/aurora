"""Process-owned writer thread for best-effort ``llm_usage_tracking`` rows.

Some usage rows are recorded by code that must not wait for the database: the input
rail sits between the user's message and the main model call, and the memory selector
runs inside a prefetch bounded to a few seconds. They used to be fire-and-forget asyncio
tasks, but a task lives and dies with its event loop. Background RCA runs under
``asyncio.run()``, which cancels whatever is still pending as soon as the workflow
returns, so those rows could be lost.

``submit`` hands the write to ONE worker thread that belongs to the process rather than
to any loop, so a write survives loop teardown. ``drain`` lets the owner of a short-lived
loop (the background chat task) wait, bounded, for what is still queued before it
returns. Interactive chat runs on a long-lived loop and never needs to drain.

The worker is a daemon thread: the interpreter does not wait for it at exit, so a hung
database write can never keep the process from shutting down. The flip side is that
whatever is still queued when the process exits is dropped; ``drain`` is the way to flush
before that point, and tracking stays best-effort past its timeout.

Tracking is best-effort: ``submit`` never raises into its caller and an exception inside
a write is logged (debug) and dropped. This module logs only the callable's name and the
exception itself; it never logs the arguments it was given.

Stdlib only on purpose: the CI pre-checks job imports the callers of this module with a
minimal dependency set.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import os
import queue
import threading
from typing import Any, Callable, NamedTuple, Optional, Set

logger = logging.getLogger(__name__)

# One worker: rows are tiny, strictly ordered writes, and a single thread keeps the DB
# load of usage tracking predictable.
_THREAD_NAME = "usage-writer"


class _Job(NamedTuple):
    future: "concurrent.futures.Future[None]"
    context: contextvars.Context
    fn: Callable[..., Any]
    args: tuple
    kwargs: dict


_lock = threading.Lock()
_queue: "queue.SimpleQueue[_Job]" = queue.SimpleQueue()
_worker: Optional[threading.Thread] = None
_pending: Set["concurrent.futures.Future[None]"] = set()


def _run_guarded(fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - usage tracking must never surface an error
        logger.debug(
            "[UsageWriter] Usage write failed (%s)",
            getattr(fn, "__qualname__", repr(fn)),
            exc_info=True,
        )


def _run_job(job: _Job) -> None:
    if not job.future.set_running_or_notify_cancel():
        return  # cancelled while it was queued: nothing to run
    try:
        job.context.run(_run_guarded, job.fn, job.args, job.kwargs)
    finally:
        # ``_run_guarded`` swallowed any exception: the future always resolves to None.
        job.future.set_result(None)


def _worker_loop(jobs: "queue.SimpleQueue[_Job]") -> None:
    """Run queued jobs one at a time, in submission order, for the life of the process."""
    while True:
        _run_job(jobs.get())


def _start_worker(jobs: "queue.SimpleQueue[_Job]") -> threading.Thread:
    worker = threading.Thread(target=_worker_loop, args=(jobs,), name=_THREAD_NAME, daemon=True)
    worker.start()
    return worker


def _ensure_worker_locked() -> None:
    """Start the worker on first use (or again if it ever died). Caller holds ``_lock``.

    Only a successfully started thread is remembered, so a failed start is retried by
    the next ``submit``.
    """
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = _start_worker(_queue)


def _forget(future: "concurrent.futures.Future[None]") -> None:
    with _lock:
        _pending.discard(future)


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> "concurrent.futures.Future[None]":
    """Queue ``fn(*args, **kwargs)`` for the writer thread and return at once.

    The call runs under a snapshot of the caller's ``contextvars`` context: a plain
    thread does not inherit the asyncio task context, and the writes rely on it (the
    input rail reads the request ids ``check_input`` published there).

    The returned future resolves to ``None`` whether or not ``fn`` raised; callers do
    not need it, ``drain`` is the way to wait.
    """
    future: "concurrent.futures.Future[None]" = concurrent.futures.Future()
    job = _Job(future, contextvars.copy_context(), fn, args, kwargs)
    try:
        with _lock:
            _ensure_worker_locked()
            _queue.put(job)
            _pending.add(future)
    except RuntimeError:
        # ``Thread.start`` refuses new threads while the interpreter shuts down.
        logger.debug("[UsageWriter] Usage write dropped: cannot start the writer thread", exc_info=True)
        future.cancel()
        return future
    # Outside the lock: a job that already finished runs the callback right here.
    future.add_done_callback(_forget)
    return future


def drain(timeout: float = 5.0) -> bool:
    """Wait up to ``timeout`` seconds for every write submitted so far to finish.

    Blocking: call it from a thread, or via ``asyncio.to_thread`` inside a coroutine.
    Returns ``True`` when nothing is left pending, ``False`` when the timeout expired
    first (the writes keep running; they are simply no longer waited for).
    """
    with _lock:
        pending = list(_pending)
    if not pending:
        return True
    _, not_done = concurrent.futures.wait(pending, timeout=timeout)
    if not_done:
        logger.warning(
            "[UsageWriter] %d usage write(s) still pending after %.1fs", len(not_done), timeout
        )
    return not not_done


def _reset_after_fork() -> None:
    """Forget the parent's writer in a forked child (e.g. a Celery prefork worker).

    The child inherits the queue and the thread object but not the running thread, so
    its queued jobs would never run (and must not run twice); it may also inherit
    ``_lock`` in a locked state. The next ``submit`` builds a fresh writer.
    """
    global _lock, _queue, _worker, _pending
    _lock = threading.Lock()
    _queue = queue.SimpleQueue()
    _worker = None
    _pending = set()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
