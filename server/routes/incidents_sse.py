"""Server-Sent Events for real-time incident updates."""
import logging
import json
import queue
from flask import Blueprint, Response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

incidents_sse_bp = Blueprint('incidents_sse', __name__)

# Store active SSE connection queues per org: {org_id: [queue1, queue2, ...]}
_active_connection_queues_by_org = {}


def broadcast_incident_update_to_user_connections(user_id: str, incident_data: dict, org_id: str = None):
    """Send incident update to all active SSE connections for a given org (or user as fallback)."""
    if org_id:
        org_queues = _active_connection_queues_by_org.get(org_id, [])
        if not org_queues:
            return
        for message_queue in org_queues:
            try:
                message_queue.put_nowait(incident_data)
            except queue.Full:
                logger.warning(f"Message queue full for org {org_id}, dropping message")
            except Exception as e:
                logger.error(f"Error sending message to queue for org {org_id}: {e}")
    else:
        org_queues = _active_connection_queues_by_org.get(user_id, [])
        if not org_queues:
            return
        for message_queue in org_queues:
            try:
                message_queue.put_nowait(incident_data)
            except queue.Full:
                logger.warning(f"Message queue full for user {user_id}, dropping message")
            except Exception as e:
                logger.error(f"Error sending message to queue for user {user_id}: {e}")


@incidents_sse_bp.route('/api/incidents/stream', methods=['GET'])
@require_permission("incidents", "read")
def incident_stream(user_id):
    """SSE endpoint that streams real-time incident updates to the client."""
    org_id = get_org_id_from_request()
    scope_key = org_id or user_id
    
    def generate_sse_events():
        message_queue = queue.Queue(maxsize=50)
        
        if scope_key not in _active_connection_queues_by_org:
            _active_connection_queues_by_org[scope_key] = []
        _active_connection_queues_by_org[scope_key].append(message_queue)
        
        try:
            while True:
                try:
                    message = message_queue.get(timeout=10)
                    yield f"data: {json.dumps(message)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if scope_key in _active_connection_queues_by_org:
                try:
                    _active_connection_queues_by_org[scope_key].remove(message_queue)
                    if not _active_connection_queues_by_org[scope_key]:
                        del _active_connection_queues_by_org[scope_key]
                except ValueError:
                    pass
    
    return Response(
        generate_sse_events(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

