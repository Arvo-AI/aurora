"""Server-Sent Events for real-time incident updates via Redis pub/sub."""
import json
import logging
import os

import redis
from flask import Blueprint, Response

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.cache.redis_client import get_redis_ssl_kwargs

logger = logging.getLogger(__name__)

incidents_sse_bp = Blueprint('incidents_sse', __name__)

_CHANNEL_PREFIX = "incidents:sse:"


def broadcast_incident_update_to_user_connections(user_id: str, incident_data: dict, org_id: str = None):
    """Publish an incident update via Redis so any process can broadcast to SSE clients."""
    scope_key = org_id or user_id
    channel = f"{_CHANNEL_PREFIX}{scope_key}"
    try:
        r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), **get_redis_ssl_kwargs())
        r.publish(channel, json.dumps(incident_data))
        r.close()
    except Exception as e:
        logger.warning("Failed to publish incident SSE update to Redis: %s", e)


@incidents_sse_bp.route('/api/incidents/stream', methods=['GET'])
@require_permission("incidents", "read")
def incident_stream(user_id):
    """SSE endpoint that streams real-time incident updates to the client."""
    org_id = get_org_id_from_request()
    scope_key = org_id or user_id
    channel = f"{_CHANNEL_PREFIX}{scope_key}"

    def generate_sse_events():
        r = None
        pubsub = None
        try:
            r = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), **get_redis_ssl_kwargs())
            pubsub = r.pubsub()
            pubsub.subscribe(channel)

            while True:
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
        generate_sse_events(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )
