"""The process-owned usage writer (utils.usage_writer).

It exists so best-effort ``llm_usage_tracking`` rows survive the event loop that scheduled
them (background RCA runs under ``asyncio.run()``, which cancels pending tasks on exit)
while never delaying, or failing, the caller. Everything is ordered with events and
``drain`` rather than sleeps.
"""

import contextvars
import logging
import queue
import threading

import pytest

from utils import usage_writer


@pytest.fixture(autouse=True)
def _idle_writer():
    """Start (and leave) every test with nothing queued, whatever ran before."""
    usage_writer.drain(timeout=5)
    yield
    usage_writer.drain(timeout=5)


@pytest.fixture
def blocker():
    """Occupy the single worker until released, so later submissions stay queued."""
    release = threading.Event()
    started = threading.Event()

    def _hold():
        started.set()
        release.wait(timeout=10)

    usage_writer.submit(_hold)
    assert started.wait(timeout=5), "the writer thread never picked up the blocking call"
    try:
        yield release
    finally:
        release.set()


def test_submit_runs_on_the_writer_thread_and_drain_waits_for_it():
    seen = []

    future = usage_writer.submit(lambda: seen.append(threading.current_thread()))

    assert usage_writer.drain(timeout=5) is True
    assert future.done()
    assert len(seen) == 1
    assert seen[0].name == "usage-writer"
    assert seen[0] is not threading.current_thread()


def test_the_writer_thread_is_a_daemon_so_a_hung_write_cannot_block_process_exit():
    """Observed from inside the worker: the interpreter must never wait for a stuck
    database write at exit (``drain`` is the bounded, explicit way to flush)."""
    seen = []

    usage_writer.submit(lambda: seen.append(threading.current_thread().daemon))

    assert usage_writer.drain(timeout=5) is True
    assert seen == [True]


def test_submit_forwards_positional_and_keyword_arguments():
    seen = []

    usage_writer.submit(lambda a, b, *, c: seen.append((a, b, c)), 1, "two", c=[3])

    assert usage_writer.drain(timeout=5) is True
    assert seen == [(1, "two", [3])]


def test_writes_run_one_at_a_time_in_submission_order():
    order = []

    for index in range(5):
        usage_writer.submit(order.append, index)

    assert usage_writer.drain(timeout=5) is True
    assert order == list(range(5))


def test_an_exception_in_the_call_is_swallowed_and_logged(caplog):
    def _boom():
        raise RuntimeError("db down")

    with caplog.at_level(logging.DEBUG, logger=usage_writer.logger.name):
        future = usage_writer.submit(_boom)
        assert usage_writer.drain(timeout=5) is True

    # The caller never sees the error: the future resolves normally.
    assert future.exception() is None
    assert future.result() is None
    records = [r for r in caplog.records if "Usage write failed" in r.getMessage()]
    assert len(records) == 1
    assert "_boom" in records[0].getMessage()
    assert isinstance(records[0].exc_info[1], RuntimeError)

    # ...and the writer keeps working afterwards.
    after = []
    usage_writer.submit(after.append, "still alive")
    assert usage_writer.drain(timeout=5) is True
    assert after == ["still alive"]


def test_the_log_never_contains_the_arguments(caplog):
    secret_argument = "sentinel-argument-value"

    def _boom(_value):
        raise RuntimeError("db down")

    with caplog.at_level(logging.DEBUG, logger=usage_writer.logger.name):
        usage_writer.submit(_boom, secret_argument)
        assert usage_writer.drain(timeout=5) is True

    assert secret_argument not in caplog.text


def test_drain_times_out_while_a_write_is_blocked_and_succeeds_once_released(blocker, caplog):
    done = []
    usage_writer.submit(done.append, "queued behind the blocker")

    with caplog.at_level(logging.WARNING, logger=usage_writer.logger.name):
        assert usage_writer.drain(timeout=0.05) is False
    assert "still pending" in caplog.text
    assert done == []

    blocker.set()
    assert usage_writer.drain(timeout=5) is True
    assert done == ["queued behind the blocker"]


def test_drain_with_nothing_pending_returns_true():
    assert usage_writer.drain(timeout=0) is True


def test_submit_snapshots_the_callers_context(blocker):
    """A plain thread does not inherit the asyncio task context; the writer must carry
    the one the caller had at submit time (the input rail reads its request ids there).
    The reader is queued behind the blocker so the later ``set`` cannot race with it."""
    var = contextvars.ContextVar("usage_writer_test_var", default="unset")
    seen = []

    token = var.set("at-submit")
    try:
        usage_writer.submit(lambda: seen.append(var.get()))
        var.set("changed-after-submit")
    finally:
        var.reset(token)

    blocker.set()
    assert usage_writer.drain(timeout=5) is True
    assert seen == ["at-submit"]


def test_submit_never_raises_when_the_writer_thread_cannot_start(monkeypatch):
    """At interpreter shutdown ``Thread.start`` raises RuntimeError; tracking is
    best-effort, so the caller gets a cancelled future instead of an exception, nothing
    is left queued, and the failed start is not remembered as a worker."""

    def _cannot_start(jobs):
        raise RuntimeError("can't create new thread at interpreter shutdown")

    monkeypatch.setattr(usage_writer, "_worker", None)
    monkeypatch.setattr(usage_writer, "_start_worker", _cannot_start)
    ran = []

    future = usage_writer.submit(ran.append, "never runs")

    assert future.cancelled()
    assert usage_writer._worker is None
    assert usage_writer.drain(timeout=1) is True
    assert ran == []


def test_a_dead_worker_is_replaced_on_the_next_submit(monkeypatch):
    """The worker is created lazily and, should it ever die, re-created the same way.
    A private queue keeps the thread started here away from the module's real worker."""
    never_started = threading.Thread(target=lambda: None, name="usage-writer-dead")
    assert not never_started.is_alive()
    monkeypatch.setattr(usage_writer, "_queue", queue.SimpleQueue())
    monkeypatch.setattr(usage_writer, "_worker", never_started)
    monkeypatch.setattr(usage_writer, "_pending", set())
    seen = []

    usage_writer.submit(lambda: seen.append(threading.current_thread()))

    assert usage_writer.drain(timeout=5) is True
    assert seen[0] is usage_writer._worker
    assert seen[0] is not never_started
    assert seen[0].daemon is True


def test_a_forked_child_forgets_the_parents_writer(monkeypatch):
    """The child inherits the queue and the thread object but not the running thread
    (and maybe a locked lock), so the after-fork hook drops all of them; the next submit
    builds a fresh writer."""
    parent_lock = usage_writer._lock
    parent_queue = usage_writer._queue
    monkeypatch.setattr(usage_writer, "_lock", parent_lock)
    monkeypatch.setattr(usage_writer, "_queue", parent_queue)
    monkeypatch.setattr(usage_writer, "_worker", usage_writer._worker)
    monkeypatch.setattr(usage_writer, "_pending", usage_writer._pending)

    usage_writer._reset_after_fork()

    assert usage_writer._worker is None
    assert usage_writer._queue is not parent_queue
    assert usage_writer._pending == set()
    assert usage_writer._lock is not parent_lock
