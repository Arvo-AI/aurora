"""Per-process cap on incident SSE streams (``routes/incidents_sse.py``).

Each SSE stream pins one gunicorn gthread thread for the life of the browser
tab. Unbounded, a few Incidents tabs took every thread, ``/health/liveness``
(which only needs a free thread) timed out and the pod was killed. The cap is
its own setting bounded by the thread budget; a tab beyond it gets a 200
``text/event-stream`` body that only sets ``retry`` and ends (EventSource
gives up for good on any non-200); streams end after a jittered lifetime so
slots rotate; and a slot is released exactly once however the stream ends.
"""

import sys
import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask


@pytest.fixture
def sse(monkeypatch):
    monkeypatch.setenv("GUNICORN_THREADS", "8")
    monkeypatch.delenv("SSE_MAX_STREAMS_PER_PROCESS", raising=False)
    sys.modules.pop("routes.incidents_sse", None)
    import routes.incidents_sse as module

    yield module
    sys.modules.pop("routes.incidents_sse", None)


@pytest.mark.parametrize(
    "streams, threads, expected",
    [
        (None, "32", 8),        # default cap
        (None, "4", 2),         # bounded by threads - 2
        (None, "8", 6),
        ("16", "32", 16),       # its own value
        ("16", "8", 6),         # its own value, still bounded by threads - 2
        ("0", "32", 1),         # never below one
        ("garbage", "garbage", 8),
    ],
)
def test_stream_cap_is_its_own_value_bounded_by_threads(monkeypatch, sse, streams, threads, expected):
    if streams is None:
        monkeypatch.delenv("SSE_MAX_STREAMS_PER_PROCESS", raising=False)
    else:
        monkeypatch.setenv("SSE_MAX_STREAMS_PER_PROCESS", streams)
    monkeypatch.setenv("GUNICORN_THREADS", threads)
    assert sse._max_streams_per_process() == expected


def test_stream_cap_defaults_without_any_env(monkeypatch, sse):
    monkeypatch.delenv("GUNICORN_THREADS", raising=False)
    monkeypatch.delenv("SSE_MAX_STREAMS_PER_PROCESS", raising=False)
    assert sse._max_streams_per_process() == 8


def test_stream_lifetime_is_jittered_around_the_base(monkeypatch, sse):
    monkeypatch.setattr(sse, "_STREAM_MAX_LIFETIME_SECONDS", 100.0)
    monkeypatch.setattr(sse.random, "uniform", lambda lo, hi: hi)
    assert sse._stream_lifetime_seconds() == pytest.approx(120.0)
    monkeypatch.setattr(sse.random, "uniform", lambda lo, hi: lo)
    assert sse._stream_lifetime_seconds() == pytest.approx(80.0)


def _one_slot(monkeypatch, sse):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sse, "_STREAM_SLOTS", sem)
    return sem


def test_iterator_releases_slot_when_stream_ends(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)

    it = sse._SlotReleasingIterator(iter(["a", "b"]))
    assert list(it) == ["a", "b"]

    assert sem.acquire(blocking=False), "slot must be free after StopIteration"


def test_iterator_releases_slot_when_the_generator_raises(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)

    def gen():
        raise ConnectionError("redis down")
        yield  # pragma: no cover - makes this a generator

    it = sse._SlotReleasingIterator(gen())
    with pytest.raises(ConnectionError):
        next(it)

    assert sem.acquire(blocking=False), "a failing stream must not leak its slot"


def test_iterator_releases_slot_on_close_without_iteration(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)

    def gen():
        yield "never consumed"

    it = sse._SlotReleasingIterator(gen())
    it.close()

    assert sem.acquire(blocking=False), "WSGI close() must release an unstarted stream"


def test_iterator_never_releases_twice(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)

    it = sse._SlotReleasingIterator(iter(["a"]))
    list(it)
    it.close()  # BoundedSemaphore raises ValueError on an extra release
    it.close()

    assert sem.acquire(blocking=False)
    assert not sem.acquire(blocking=False)


def _fake_redis(monkeypatch, sse):
    pubsub = MagicMock(name="pubsub")
    pubsub.get_message.return_value = None  # -> keepalive
    client = MagicMock(name="redis")
    client.pubsub.return_value = pubsub
    monkeypatch.setattr(sse.redis, "from_url", lambda *a, **k: client)
    monkeypatch.setattr(sse, "get_redis_ssl_kwargs", lambda: {})
    return client, pubsub


def _call_view(sse):
    app = Flask(__name__)
    with app.test_request_context("/api/incidents/stream"):
        # ``__wrapped__`` skips the RBAC decorator (covered by tests/auth).
        return sse.incident_stream.__wrapped__("user-1")


def test_view_refuses_with_a_retry_only_stream_when_no_slot_is_free(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)
    monkeypatch.setattr(sse, "get_org_id_from_request", lambda: "org-1")
    _fake_redis(monkeypatch, sse)

    resp = _call_view(sse)

    # A non-200 status makes EventSource fail the connection for good; a 200
    # stream that ends cleanly is what makes it reconnect after ``retry`` ms.
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.get_data(as_text=True) == f"retry: {sse._STREAM_REFUSED_RETRY_MS}\n\n"
    assert not sem.acquire(blocking=False), "refusal must not consume the held slot"


def test_view_streams_then_releases_slot_on_close(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    monkeypatch.setattr(sse, "get_org_id_from_request", lambda: "org-1")
    client, pubsub = _fake_redis(monkeypatch, sse)

    resp = _call_view(sse)

    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert not sem.acquire(blocking=False), "an open stream holds the slot"

    body = resp.response
    assert isinstance(body, sse._SlotReleasingIterator)
    assert next(body) == f"retry: {sse._STREAM_RETRY_MS}\n\n"
    assert next(body) == ": keepalive\n\n"
    body.close()

    assert sem.acquire(blocking=False), "slot released when the client disconnects"
    pubsub.unsubscribe.assert_called_once()
    client.close.assert_called_once()


def test_view_ends_stream_after_max_lifetime(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    monkeypatch.setattr(sse, "get_org_id_from_request", lambda: "org-1")
    monkeypatch.setattr(sse, "_STREAM_MAX_LIFETIME_SECONDS", 0.0)
    _fake_redis(monkeypatch, sse)

    resp = _call_view(sse)
    chunks = list(resp.response)

    assert chunks == [f"retry: {sse._STREAM_RETRY_MS}\n\n"]
    assert sem.acquire(blocking=False), "slot released when the lifetime ends"
