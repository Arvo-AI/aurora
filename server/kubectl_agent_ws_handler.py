import json
import logging
import uuid
import threading
from datetime import datetime, timezone
from typing import Dict, Any
import websockets
from utils.db.db_adapters import connect_to_db_as_admin

logger = logging.getLogger(__name__)

_agent_websockets: Dict[str, Dict[str, Any]] = {}
_command_handlers: Dict[str, Any] = {}
_ws_lock = threading.Lock()
_handlers_lock = threading.Lock()

def _execute_query(query, params):
    conn = connect_to_db_as_admin()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
    finally:
        if cursor:
            cursor.close()
        conn.close()

def get_agent_websocket_by_cluster(user_id: str, cluster_identifier: str):
    """
    Get websocket for cluster by cluster_id.
    
    Args:
        user_id: User ID for ownership verification
        cluster_identifier: The cluster_id
    
    Returns:
        WebSocket connection or None if not found/unauthorized
    """
    conn = connect_to_db_as_admin()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.cluster_id, t.user_id
            FROM active_kubectl_connections c
            JOIN kubectl_agent_tokens t ON c.token = t.token
            WHERE c.cluster_id = %s AND c.status = 'active'
        """, (cluster_identifier,))
        result = cursor.fetchone()
        
        if not result:
            logger.warning(f"No active cluster found for identifier: {cluster_identifier}")
            return None
            
        cluster_id, owner_id = result
        if owner_id != user_id:
            logger.warning(f"Unauthorized cluster access attempt by {user_id} for cluster {cluster_id}")
            return None
        
        with _ws_lock:
            websocket_info = _agent_websockets.get(cluster_id)
            return websocket_info['websocket'] if websocket_info else None
        
    except Exception as e:
        logger.error(f"Error looking up cluster {cluster_identifier}: {e}", exc_info=True)
        return None
    finally:
        if cursor:
            cursor.close()
        conn.close()

def register_command_response_handler(command_id: str, handler: Any):
    with _handlers_lock:
        _command_handlers[command_id] = handler

def unregister_command_response_handler(command_id: str):
    with _handlers_lock:
        _command_handlers.pop(command_id, None)

async def handle_kubectl_agent(websocket) -> None:
    cluster_id = None
    cluster_name = None
    user_id = None
    try:
        auth_header = websocket.request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            await websocket.close(code=1008, reason="Missing Authorization header")
            return
        token = auth_header.replace('Bearer ', '').strip()
        cluster_id = websocket.request.headers.get('X-Cluster-ID', f'cluster-{uuid.uuid4().hex[:8]}')
        conn = connect_to_db_as_admin()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT cluster_name, status, expires_at, user_id FROM kubectl_agent_tokens WHERE token = %s", (token,))
            result = cursor.fetchone()
            if not result:
                await websocket.close(code=1008, reason="Invalid token")
                return
            cluster_name, status, expires_at, user_id = result
            if status != 'active':
                await websocket.close(code=1008, reason="Token revoked")
                return
            if expires_at and expires_at < datetime.now(timezone.utc):
                await websocket.close(code=1008, reason="Token expired")
                return
            cursor.execute("""
                INSERT INTO active_kubectl_connections (cluster_id, token, connected_at, last_heartbeat, status)
                VALUES (%s, %s, NOW(), NOW(), 'active')
                ON CONFLICT (cluster_id) DO UPDATE SET token = EXCLUDED.token, connected_at = NOW(), last_heartbeat = NOW(), status = 'active'
            """, (cluster_id, token))
            cursor.execute("UPDATE kubectl_agent_tokens SET last_connected_at = NOW(), cluster_id = %s WHERE token = %s", (cluster_id, token))
            conn.commit()
        finally:
            if cursor:
                cursor.close()
            conn.close()
        
        with _ws_lock:
            _agent_websockets[cluster_id] = {'websocket': websocket, 'user_id': user_id}
        logger.info(f"kubectl agent connected: {cluster_name} ({cluster_id})")
        await websocket.send(json.dumps({'type': 'connected', 'cluster_id': cluster_id, 'cluster_name': cluster_name}))
        
        async for message_text in websocket:
            try:
                message = json.loads(message_text)
                msg_type = message.get('type')
                if msg_type == 'heartbeat':
                    _execute_query("UPDATE active_kubectl_connections SET last_heartbeat = NOW(), status = 'active' WHERE cluster_id = %s", (cluster_id,))
                    await websocket.send(json.dumps({'type': 'heartbeat_ack'}))
                elif msg_type == 'register':
                    agent_version = message.get('agent_version', 'unknown')
                    _execute_query(
                        "UPDATE active_kubectl_connections SET agent_version = %s WHERE cluster_id = %s",
                        (agent_version, cluster_id)
                    )
                    logger.info(f"kubectl agent registered: {cluster_name} v{agent_version}")
                elif msg_type == 'command_response':
                    command_id = message.get('command_id')
                    with _handlers_lock:
                        handler = _command_handlers.get(command_id)
                    if handler:
                        result = {
                            'success': message.get('success', False),
                            'output': message.get('output', ''),
                            'error': message.get('error'),
                            'return_code': message.get('return_code', 1)
                        }
                        if hasattr(handler, 'put_nowait'):
                            handler.put_nowait(('success', result))
                        elif hasattr(handler, 'done') and not handler.done():
                            handler.set_result(result)
                    elif command_id:
                        logger.warning(f"Received response for unknown command_id: {command_id}")
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from kubectl agent {cluster_id}")
            except Exception as e:
                logger.error(f"Error handling kubectl agent message: {e}")
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"kubectl agent disconnected: {cluster_id}")
    except Exception as e:
        logger.error(f"kubectl agent connection error: {e}", exc_info=True)
    finally:
        if cluster_id:
            with _ws_lock:
                _agent_websockets.pop(cluster_id, None)
            try:
                _execute_query("UPDATE active_kubectl_connections SET status = 'stale' WHERE cluster_id = %s", (cluster_id,))
            except Exception as e:
                logger.error(f"Error marking kubectl agent as stale: {e}")
