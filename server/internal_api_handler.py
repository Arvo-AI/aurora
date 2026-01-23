"""Internal API handler for chatbot service HTTP endpoints."""
import asyncio
import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)


async def handle_http_request(reader, writer):
    """Handle HTTP health checks and internal kubectl API requests."""
    try:
        request_line = (await reader.readline()).decode().strip()
        headers = {}
        while True:
            line = (await reader.readline()).decode().strip()
            if not line:
                break
            if ': ' in line:
                key, value = line.split(': ', 1)
                headers[key.lower()] = value
        
        body = b''
        if 'content-length' in headers:
            body = await reader.read(int(headers['content-length']))
        
        # Health check
        if request_line.startswith('GET /health'):
            await _send_response(writer, "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            return
        
        # Internal kubectl API
        if request_line.startswith('POST /internal/kubectl/execute'):
            await _handle_kubectl_execute(headers, body, writer)
            return
        
        # 404
        await _send_response(writer, "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot Found")
    except Exception as e:
        logger.error(f"HTTP handler error: {e}", exc_info=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_kubectl_execute(headers, body, writer):
    """Handle internal kubectl execution endpoint."""
    if headers.get('x-internal-secret') != os.getenv('FLASK_SECRET_KEY'):
        await _send_json_response(writer, {"error": "Unauthorized"}, status="403 Forbidden")
        return
    
    from kubectl_agent_ws_handler import get_agent_websocket_by_cluster, register_command_response_handler, unregister_command_response_handler
    
    data = json.loads(body.decode())
    user_id, cluster_id, command = data['user_id'], data['cluster_id'], data['command']
    timeout = data.get('timeout', 60)
    
    websocket = get_agent_websocket_by_cluster(user_id, cluster_id)
    if not websocket:
        await _send_json_response(writer, {'success': False, 'error': f"No active agent for cluster '{cluster_id}'"})
        return
    
    command_id = str(uuid.uuid4())
    future = asyncio.Future()
    register_command_response_handler(command_id, future)
    
    try:
        # Re-check websocket is still valid before sending
        websocket = get_agent_websocket_by_cluster(user_id, cluster_id)
        if not websocket:
            await _send_json_response(writer, {'success': False, 'error': f"Agent disconnected for cluster '{cluster_id}'"})
            return
        
        await websocket.send(json.dumps({'type': 'kubectl_command', 'command_id': command_id, 'command': command, 'timeout': timeout}))
        result = await asyncio.wait_for(future, timeout=timeout)
        await _send_json_response(writer, result)
    except asyncio.TimeoutError:
        await _send_json_response(writer, {'success': False, 'error': 'No response from agent'})
    except Exception as e:
        logger.error(f"Error communicating with kubectl agent: {e}", exc_info=True)
        await _send_json_response(writer, {'success': False, 'error': f'Agent communication error: {str(e)}'})
    finally:
        unregister_command_response_handler(command_id)


async def _send_response(writer, response_str):
    """Send raw HTTP response."""
    writer.write(response_str.encode())
    await writer.drain()


async def _send_json_response(writer, data, status="200 OK"):
    """Send JSON HTTP response."""
    result = json.dumps(data)
    response = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(result)}\r\nConnection: close\r\n\r\n{result}"
    await _send_response(writer, response)

