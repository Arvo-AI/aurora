"""Server-Sent Events for real-time incident updates."""
import logging
import json
import queue
from flask import Blueprint, Response
from utils.auth.stateless_auth import get_user_id_from_request

logger = logging.getLogger(__name__)

incidents_sse_bp = Blueprint('incidents_sse', __name__)

# Store active SSE connection queues per user: {user_id: [queue1, queue2, ...]}
_active_connection_queues_by_user = {}


def broadcast_incident_update_to_user_connections(user_id: str, incident_data: dict):
    """Send incident update to all active SSE connections for a given user."""
    user_queues = _active_connection_queues_by_user.get(user_id, [])
    if not user_queues:
        return
    
    for message_queue in user_queues:
        try:
            message_queue.put_nowait(incident_data)
        except queue.Full:
            logger.warning(f"Message queue full for user {user_id}, dropping message")
        except Exception as e:
            logger.error(f"Error sending message to queue for user {user_id}: {e}")


@incidents_sse_bp.route('/api/incidents/stream', methods=['GET'])
def incident_stream():
    """SSE endpoint that streams real-time incident updates to the client."""
    user_id = get_user_id_from_request()
    if not user_id:
        return Response("Unauthorized", status=401)
    
    def generate_sse_events():
        # Create a message queue for this connection
        message_queue = queue.Queue(maxsize=50)
        
        # Register this queue with the user's active connections
        if user_id not in _active_connection_queues_by_user:
            _active_connection_queues_by_user[user_id] = []
        _active_connection_queues_by_user[user_id].append(message_queue)
        
        try:
            # Continuously read messages and yield as SSE events
            while True:
                try:
                    message = message_queue.get(timeout=10)
                    yield f"data: {json.dumps(message)}\n\n"
                except queue.Empty:
                    # Send keepalive comment to prevent proxy/client timeouts
                    yield ": keepalive\n\n"
        except GeneratorExit:
            # Client disconnected
            pass
        finally:
            # Clean up: remove this queue from user's connections
            if user_id in _active_connection_queues_by_user:
                try:
                    _active_connection_queues_by_user[user_id].remove(message_queue)
                    # Remove user entry if no more connections
                    if not _active_connection_queues_by_user[user_id]:
                        del _active_connection_queues_by_user[user_id]
                except ValueError:
                    # Queue already removed, ignore
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

