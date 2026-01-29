"""SSE endpoint for streaming visualization updates."""
import json
import logging
import os
from flask import Blueprint, Response, jsonify, stream_with_context
import redis
from utils.auth.stateless_auth import get_user_id_from_request
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

visualization_bp = Blueprint('visualization', __name__)


@visualization_bp.route('/api/incidents/<incident_id>/visualization/stream', methods=['GET'])
def stream_visualization_updates(incident_id: str):
    """SSE endpoint for real-time visualization updates."""
    user_id = get_user_id_from_request()
    if not user_id:
        return Response("Unauthorized", status=401)
    
    # Verify incident ownership
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM incidents WHERE id = %s AND user_id = %s", (incident_id, user_id))
                if not cursor.fetchone():
                    return Response("Forbidden", status=403)
    except Exception as e:
        logger.error(f"[Visualization] Auth check failed: {e}")
        return Response("Internal error", status=500)
    
    def event_stream():
        redis_client = None
        pubsub = None
        try:
            redis_client = redis.from_url(os.getenv('REDIS_URL', 'redis://redis:6379/0'))
            pubsub = redis_client.pubsub()
            channel = f"visualization:{incident_id}"
            pubsub.subscribe(channel)
            
            yield f"data: {json.dumps({'type': 'connected', 'incident_id': incident_id})}\n\n"
            
            # Use get_message with timeout instead of listen() to avoid blocking forever
            while True:
                message = pubsub.get_message(timeout=30.0)
                if message and message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    yield f"data: {data}\n\n"
                elif message is None:
                    # Timeout - send heartbeat to detect disconnects
                    yield f": heartbeat\n\n"
        finally:
            if pubsub:
                pubsub.unsubscribe(channel)
                pubsub.close()
            if redis_client:
                redis_client.close()
    
    return Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@visualization_bp.route('/api/incidents/<incident_id>/visualization', methods=['GET'])
def get_current_visualization(incident_id: str):
    """Fetch current visualization JSON."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT visualization_code, visualization_updated_at
                    FROM incidents
                    WHERE id = %s AND user_id = %s
                """, (incident_id, user_id))
                
                row = cursor.fetchone()
        
        if not row or not row[0]:
            return jsonify({"error": "No visualization found"}), 404
        
        viz_data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        
        return jsonify({
            "data": viz_data,
            "updatedAt": row[1].isoformat() if row[1] else None,
        })
    
    except Exception as e:
        logger.error(f"[Visualization] Failed to fetch viz: {e}")
        return jsonify({"error": str(e)}), 500
