"""Internal API handler for chatbot service HTTP endpoints."""
import asyncio
import hmac
import json
import logging
import os
import shlex
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
    internal_secret = os.getenv('INTERNAL_API_SECRET') or ''
    provided = headers.get('x-internal-secret') or ''
    if internal_secret and not hmac.compare_digest(internal_secret, provided):
        await _send_json_response(writer, {"error": "Unauthorized"}, status="403 Forbidden")
        return
    
    from utils.kubectl.agent_ws_handler import get_agent_websocket_by_cluster, register_command_response_handler, unregister_command_response_handler
    
    data = json.loads(body.decode())
    user_id, cluster_id, command = data['user_id'], data['cluster_id'], data['command']
    timeout = data.get('timeout', 60)
    
    # Try WebSocket agent first (existing path)
    websocket = get_agent_websocket_by_cluster(user_id, cluster_id)
    if websocket:
        command_id = str(uuid.uuid4())
        future = asyncio.Future()
        register_command_response_handler(command_id, future)
        try:
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
        return
    
    # Fallback: try kubeconfig-based direct execution
    try:
        kubeconfig_info = _get_kubeconfig_for_cluster(user_id, cluster_id)
        if kubeconfig_info:
            result = await _execute_kubectl_with_kubeconfig(
                command, kubeconfig_info['yaml'], kubeconfig_info['context_name'], timeout
            )
            await _send_json_response(writer, result)
            return
    except Exception:
        logger.exception("Kubeconfig fallback failed for cluster %s", cluster_id)
        await _send_json_response(writer, {'success': False, 'error': 'Kubeconfig execution failed'})
        return
    
    await _send_json_response(writer, {'success': False, 'error': f"No active agent or kubeconfig for cluster '{cluster_id}'"})


def _get_kubeconfig_for_cluster(user_id: str, cluster_id: str):
    """Look up kubeconfig YAML from Vault for a given cluster_id."""
    from utils.db.db_utils import connect_to_db_as_admin
    from utils.auth.token_management import get_token_data
    from utils.auth.stateless_auth import resolve_org_id, set_rls_context

    conn = connect_to_db_as_admin()
    try:
        cursor = conn.cursor()
        org_id = resolve_org_id(user_id)
        set_rls_context(cursor, conn, user_id, log_prefix="[KubeconfigExec]")
        cursor.execute("""
            SELECT vault_provider, context_name FROM kubeconfig_clusters
            WHERE cluster_id = %s AND org_id = %s AND is_active = TRUE
        """, (cluster_id, org_id))
        row = cursor.fetchone()
        if not row:
            return None
        vault_provider, context_name = row
    finally:
        cursor.close()
        conn.close()

    token_data = get_token_data(user_id, vault_provider, org_id=org_id)
    if not token_data or 'kubeconfig_yaml' not in token_data:
        return None
    return {'yaml': token_data['kubeconfig_yaml'], 'context_name': context_name}


async def _execute_kubectl_with_kubeconfig(command: str, kubeconfig_yaml: str, context_name: str, timeout_seconds: int) -> dict:
    """Execute kubectl using a kubeconfig retrieved from Vault."""
    import tempfile

    private_dir = os.path.join(tempfile.gettempdir(), 'aurora_kubeconfig')
    os.makedirs(private_dir, mode=0o700, exist_ok=True)

    invocation_home = tempfile.mkdtemp(prefix='aurora_home_', dir=private_dir)
    fd, kubeconfig_path = tempfile.mkstemp(prefix='aurora_kc_', suffix='.yaml', dir=private_dir)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, kubeconfig_yaml.encode())
        os.close(fd)
    except Exception:
        os.close(fd)
        os.unlink(kubeconfig_path)
        raise

    try:
        exec_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": invocation_home,
            "KUBECONFIG": kubeconfig_path,
        }
        cmd_parts = ["kubectl", f"--context={context_name}"] + shlex.split(command)

        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=exec_env,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {'success': False, 'error': f'Command timed out after {timeout_seconds}s', 'return_code': 124}

        return {
            'success': proc.returncode == 0,
            'output': stdout.decode(errors='replace'),
            'error': stderr.decode(errors='replace') if proc.returncode != 0 else None,
            'return_code': proc.returncode,
        }
    finally:
        try:
            os.unlink(kubeconfig_path)
        except OSError as e:
            logger.debug("Failed to remove temporary kubeconfig file %s: %s", kubeconfig_path, e)
        try:
            import shutil
            shutil.rmtree(invocation_home, ignore_errors=True)
        except OSError:
            pass


async def _send_response(writer, response_str):
    """Send raw HTTP response."""
    writer.write(response_str.encode())
    await writer.drain()


async def _send_json_response(writer, data, status="200 OK"):
    """Send JSON HTTP response."""
    result = json.dumps(data)
    response = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(result)}\r\nConnection: close\r\n\r\n{result}"
    await _send_response(writer, response)

