import json
import logging
import os
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def on_prem_kubectl(
    command: str,
    cluster_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 60
) -> str:
    """
    Execute kubectl command on an on-prem cluster via connected agent.
    
    Args:
        command: kubectl command (with or without 'kubectl' prefix)
        cluster_id: Target cluster ID (unique identifier)
        user_id: User context (auto-injected)
        session_id: Session context (auto-injected)
        timeout: Command timeout in seconds (default: 60)
    
    Returns:
        JSON string with execution results
    """
    if not user_id:
        return json.dumps({
            'success': False,
            'error': 'User context required',
            'chat_output': 'Authentication required',
            'command': command,
            'return_code': 1,
            'provider': 'onprem_kubectl'
        })
    
    # Normalize command
    command = command.strip()
    if command.lower().startswith('kubectl '):
        command = command[8:].strip()

    # Org command policy check against full kubectl command
    from utils.auth.command_policy import evaluate_compound_command
    from utils.auth.stateless_auth import get_org_id_for_user
    full_command = f"kubectl {command}"
    org_id = get_org_id_for_user(user_id) if user_id else None
    verdict = evaluate_compound_command(org_id, full_command)
    if not verdict.allowed:
        reason = (verdict.rule_description or "Matched organization policy")[:200]
        logger.warning("Policy denied kubectl_onprem command for user %s (%s)",
                        user_id, reason)
        return json.dumps({
            'success': False,
            'error': f"Command blocked by organization policy: {reason}",
            'code': 'POLICY_DENIED',
            'chat_output': f"$ {full_command}\nBlocked by organization policy: {reason}",
            'command': full_command,
            'return_code': 1,
            'provider': 'onprem_kubectl',
        })

    from utils.security.signature_match import check_signature
    sig = check_signature(full_command)
    if sig.matched:
        return json.dumps({
            'success': False,
            'error': f"Command blocked: {sig.description}",
            'code': 'SIGNATURE_MATCHED',
            'chat_output': f"$ {full_command}\nBlocked: {sig.description}",
            'command': full_command,
            'return_code': 1,
            'provider': 'onprem_kubectl',
        })

    from utils.security.command_safety import check_command_safety
    verdict = check_command_safety(full_command, tool_name="kubectl_onprem", user_id=user_id, session_id=session_id)
    if verdict.conclusion:
        return json.dumps({
            'success': False,
            'error': f"Command blocked by safety guardrail: {verdict.thought}",
            'code': 'SAFETY_BLOCKED',
            'chat_output': f"$ {full_command}\nBlocked by safety guardrail: {verdict.thought}",
            'command': full_command,
            'return_code': 1,
            'provider': 'onprem_kubectl',
        })

    # Call internal API on chatbot service
    try:
        chatbot_url = os.getenv('CHATBOT_INTERNAL_URL')
        if not chatbot_url:
            return json.dumps({
                'success': False,
                'error': 'CHATBOT_INTERNAL_URL not configured',
                'chat_output': f'$ kubectl {command}\nError: CHATBOT_INTERNAL_URL environment variable not set',
                'command': f'kubectl {command}',
                'return_code': 1,
                'provider': 'onprem_kubectl'
            })
        
        response = requests.post(
            f'{chatbot_url}/internal/kubectl/execute',
            json={'user_id': user_id, 'cluster_id': cluster_id, 'command': command, 'timeout': timeout},
            headers={'X-Internal-Secret': os.getenv('INTERNAL_API_SECRET')},
            timeout=timeout
        )
        
        # Check response status before parsing JSON
        if not response.ok:
            error_msg = f"HTTP {response.status_code}"
            try:
                error_data = response.json()
                error_msg = error_data.get('error', error_msg)
            except:
                error_msg = response.text[:200] if response.text else error_msg
            
            return json.dumps({
                'success': False,
                'error': error_msg,
                'chat_output': f'$ kubectl {command}\nError: {error_msg}',
                'command': f'kubectl {command}',
                'return_code': response.status_code,
                'provider': 'onprem_kubectl'
            })
        
        result = response.json()
    except requests.exceptions.Timeout:
        return json.dumps({
            'success': False,
            'error': 'Command execution timeout',
            'chat_output': f'$ kubectl {command}\nError: Command execution timeout',
            'command': f'kubectl {command}',
            'return_code': 1,
            'provider': 'onprem_kubectl'
        })
    except Exception as e:
        logger.error(f"Error calling chatbot internal API: {e}", exc_info=True)
        return json.dumps({
            'success': False,
            'error': f'Internal error: {str(e)}',
            'chat_output': f'$ kubectl {command}\nError: Internal error: {str(e)}',
            'command': f'kubectl {command}',
            'return_code': 1,
            'provider': 'onprem_kubectl'
        })
    
    # Format response - match cloud_exec/terminal_exec pattern
    full_command = f"kubectl {command}"
    response_data = {
        'success': result.get('success', False),
        'command': full_command,
        'final_command': full_command,
        'return_code': result.get('return_code', 1 if not result.get('success') else 0),
        'provider': 'onprem_kubectl'
    }
    
    if result.get('success'):
        output = result.get('output', '').strip()
        response_data['output'] = output
        response_data['chat_output'] = f"$ {full_command}\n{output}" if output else f"$ {full_command}\n(no output)"
    else:
        error = result.get('error', 'Unknown error')
        response_data['error'] = error
        response_data['chat_output'] = f"$ {full_command}\nError: {error}"
    
    return json.dumps(response_data)


def is_kubectl_onprem_connected(user_id: str) -> bool:
    """Check if user has active on-prem kubectl connections."""
    if not user_id:
        return False
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[KubectlOnprem:check]")
                cursor.execute(
                    """SELECT COUNT(*) FROM active_kubectl_connections c
                       JOIN kubectl_agent_tokens t ON c.token = t.token
                       WHERE t.user_id = %s AND c.status = 'active'""",
                    (user_id,),
                )
                count = cursor.fetchone()[0]
                return count > 0
    except Exception:
        logger.debug("kubectl on-prem connection check failed for user %s", user_id)
        return False
