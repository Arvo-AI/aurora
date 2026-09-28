"""Server-Sent Events for real-time incident updates via Redis pub/sub."""
import json
import logging
import os
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


def _max_streams_per_process() -> int:
    """Streams a single gunicorn worker process may hold open at once.

    One SSE stream pins one gthread thread for as long as the browser tab is
    open, and the server runs a fixed thread budget (GUNICORN_THREADS per
    worker). Left unbounded, a handful of Incidents tabs took every thread and
    the liveness probe, which needs one free thread, timed out and the pod was
    killed. Keep streams to a quarter of the budget so requests and probes
    always have threads left.
    """
    try:
        threads = int(os.getenv("GUNICORN_THREADS", "32"))
    except ValueError:
        threads = 32
    return max(2, threads // 4)


_STREAM_SLOTS = threading.BoundedSemaphore(_max_streams_per_process())

# End every stream after this long. The browser's EventSource reconnects
# immediately, so a tab that was refused a slot earlier gets a fair chance at
# one, and no single tab can hold a thread for hours.
_STREAM_MAX_LIFETIME_SECONDS = 300.0

# Sent when no slot is free. EventSource treats 503 as "reconnect after the
# retry interval" (~3s in browsers), so the tab keeps retrying without holding
# a thread in the meantime.
_STREAM_RETRY_AFTER_SECONDS = "15"


class _SlotReleasingIterator:
    """Wraps the SSE generator so the slot is released exactly once, whether
    the stream ends, the client disconnects (WSGI ``close()``), or the body is
    never iterated at all."""

    def __init__(self, generator):
        self._generator = generator
        self._released = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._generator)
        except StopIteration:
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
            "SSE stream slots exhausted in this process (limit %d); asking client to retry",
            _max_streams_per_process(),
        )
        return Response(
            status=503,
            headers={
                'Retry-After': _STREAM_RETRY_AFTER_SECONDS,
                'Cache-Control': 'no-cache',
            },
        )

    def generate_sse_events():
        r = None
        pubsub = None
        started = time.monotonic()
        try:
            r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), **get_redis_ssl_kwargs())
            pubsub = r.pubsub()
            pubsub.subscribe(channel)

            while time.monotonic() - started < _STREAM_MAX_LIFETIME_SECONDS:
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
