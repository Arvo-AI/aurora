"""Per-process cap on incident SSE streams (``routes/incidents_sse.py``).

Each SSE stream pins one gunicorn gthread thread for the life of the browser
tab. Unbounded, a few Incidents tabs took every thread, ``/health/liveness``
(which only needs a free thread) timed out and the pod was killed. The cap
keeps streams to a quarter of the thread budget, refuses extra streams with a
503 the browser retries, ends streams after a fixed lifetime so slots rotate,
and releases a slot exactly once however the stream ends.
"""

import sys
import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask


@pytest.fixture
def sse(monkeypatch):
    monkeypatch.setenv("GUNICORN_THREADS", "8")
    sys.modules.pop("routes.incidents_sse", None)
    import routes.incidents_sse as module

    yield module
    sys.modules.pop("routes.incidents_sse", None)


@pytest.mark.parametrize(
    "threads, expected",
    [("4", 2), ("8", 2), ("16", 4), ("32", 8), ("64", 16), ("not-a-number", 8)],
)
def test_stream_cap_is_a_quarter_of_the_thread_budget(monkeypatch, sse, threads, expected):
    monkeypatch.setenv("GUNICORN_THREADS", threads)
    assert sse._max_streams_per_process() == expected


def test_stream_cap_defaults_to_the_chart_thread_default(monkeypatch, sse):
    monkeypatch.delenv("GUNICORN_THREADS", raising=False)
    assert sse._max_streams_per_process() == 32 // 4


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


def test_view_refuses_with_503_when_no_slot_is_free(monkeypatch, sse):
    sem = _one_slot(monkeypatch, sse)
    assert sem.acquire(blocking=False)
    monkeypatch.setattr(sse, "get_org_id_from_request", lambda: "org-1")
    _fake_redis(monkeypatch, sse)

    resp = _call_view(sse)

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == sse._STREAM_RETRY_AFTER_SECONDS
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

    assert chunks == []
    assert sem.acquire(blocking=False), "slot released when the lifetime ends"
