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
            headers={'X-Internal-Secret': os.getenv('FLASK_SECRET_KEY')},
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
