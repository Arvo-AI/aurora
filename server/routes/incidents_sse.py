"""Server-Sent Events for real-time incident updates via Redis pub/sub."""
import json
import logging
import os
import random
import threading
import time

import redis
from flask import Blueprint, Response

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.cache.redis_client import get_redis_client, get_redis_ssl_kwargs

logger = logging.getLogger(__name__)

incidents_sse_bp = Blueprint('incidents_sse', __name__)

_CHANNEL_PREFIX = "incidents:sse:"

_DEFAULT_MAX_STREAMS = 8
# Threads a worker process always keeps free for ordinary requests and probes.
_RESERVED_THREADS = 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _max_streams_per_process() -> int:
    """Streams a single gunicorn worker process may hold open at once.

    One SSE stream pins one gthread thread for as long as the browser tab is
    open (the Incidents list and every incident page each open one). Left
    unbounded, a handful of tabs took every thread and the liveness probe,
    which needs one free thread, timed out and the pod was killed.

    The cap is ``SSE_MAX_STREAMS_PER_PROCESS`` (default 8): open tabs are a
    product of how many people use the install, not of request concurrency,
    so it is sized on its own. It never exceeds ``GUNICORN_THREADS - 2`` so
    requests and probes always have threads left.
    """
    wanted = _env_int("SSE_MAX_STREAMS_PER_PROCESS", _DEFAULT_MAX_STREAMS)
    threads = _env_int("GUNICORN_THREADS", 32)
    return max(1, min(wanted, threads - _RESERVED_THREADS))


_STREAM_SLOTS = threading.BoundedSemaphore(_max_streams_per_process())

# End every stream after roughly this long, +-20% jitter so tabs that opened
# together do not all expire and race for slots at the same instant. The
# browser's EventSource reconnects after a clean end of stream, so a tab that
# was refused a slot earlier gets a fair chance at one, and no single tab can
# hold a thread for hours.
_STREAM_MAX_LIFETIME_SECONDS = 300.0
_STREAM_LIFETIME_JITTER = 0.2

# Reconnection delay handed to a tab that could not get a slot. Any non-200
# response makes EventSource give up for good (readyState CLOSED, no retry),
# so the refusal is a 200 text/event-stream body that only sets the retry
# interval and ends: the browser reconnects after ``retry`` ms and holds no
# thread in the meantime.
_STREAM_REFUSED_RETRY_MS = 15000
# Accepted streams reset the interval so a tab reconnects quickly once its
# lifetime ends.
_STREAM_RETRY_MS = 2000


def _stream_lifetime_seconds() -> float:
    jitter = 1.0 + random.uniform(-_STREAM_LIFETIME_JITTER, _STREAM_LIFETIME_JITTER)
    return _STREAM_MAX_LIFETIME_SECONDS * jitter


class _SlotReleasingIterator:
    """Wraps the SSE generator so the slot is released exactly once, whether
    the stream ends, the generator raises, the client disconnects (WSGI
    ``close()``), or the body is never iterated at all."""

    def __init__(self, generator):
        self._generator = generator
        self._released = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._generator)
        except BaseException:
            # StopIteration, a Redis error, GeneratorExit: the stream is over
            # either way, and the slot must not depend on the WSGI server
            # calling close() afterwards.
            self._release()
            raise

    def close(self):
        try:
            close = getattr(self._generator, "close", None)
            if close is not None:
                close()
        finally:
            self._release()

    def _release(self):
        if not self._released:
            self._released = True
            _STREAM_SLOTS.release()


def broadcast_incident_update_to_user_connections(user_id: str, incident_data: dict, org_id: str = None):
    """Publish an incident update via Redis so any process can broadcast to SSE clients."""
    scope_key = org_id or user_id
    channel = f"{_CHANNEL_PREFIX}{scope_key}"
    try:
        r = get_redis_client()
        if r:
            r.publish(channel, json.dumps(incident_data))
    except Exception as e:
        logger.warning("Failed to publish incident SSE update to Redis: %s", e)


@incidents_sse_bp.route('/api/incidents/stream', methods=['GET'])
@require_permission("incidents", "read")
def incident_stream(user_id):
    """SSE endpoint that streams real-time incident updates to the client."""
    org_id = get_org_id_from_request()
    scope_key = org_id or user_id
    channel = f"{_CHANNEL_PREFIX}{scope_key}"

    if not _STREAM_SLOTS.acquire(blocking=False):
        logger.warning(
            "SSE stream slots exhausted in this process (limit %d); asking client to retry in %dms",
            _max_streams_per_process(), _STREAM_REFUSED_RETRY_MS,
        )
        return Response(
            f"retry: {_STREAM_REFUSED_RETRY_MS}\n\n",
            status=200,
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )

    def generate_sse_events():
        r = None
        pubsub = None
        started = time.monotonic()
        lifetime = _stream_lifetime_seconds()
        try:
            yield f"retry: {_STREAM_RETRY_MS}\n\n"
            r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), **get_redis_ssl_kwargs())
            pubsub = r.pubsub()
            pubsub.subscribe(channel)

            while time.monotonic() - started < lifetime:
                message = pubsub.get_message(timeout=10.0)
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                elif message is None:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if pubsub:
                pubsub.unsubscribe(channel)
                pubsub.close()
            if r:
                r.close()

    return Response(
        _SlotReleasingIterator(generate_sse_events()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )
