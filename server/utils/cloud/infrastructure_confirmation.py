from __future__ import annotations

"""Shared helper for interactive user confirmation before running destructive cloud actions."""

import logging
import time
import uuid
from typing import Dict, Any
# Lazy imports to avoid circular dependency with cloud_tools.py

logger = logging.getLogger(__name__)

# Global store for pending confirmations - maps confirmation_id to result + metadata
_pending_confirmations: Dict[str, Dict[str, Any]] = {}

# We import send_websocket_message lazily to avoid circular imports

def _send_ws(payload: dict, tool_name: str):
    try:
        logger.debug(f"WEBSOCKET: Sending confirmation via WebSocket: {payload.get('data', {}).get('confirmation_id')} for tool {tool_name}")
        # Lazy import to avoid circular dependency
        from chat.backend.agent.tools.cloud_tools import send_websocket_message
        send_websocket_message(
            payload,
            tool_name=tool_name,
            fallback_message="Awaiting user confirmation...",
        )
    except Exception as e:
        logger.error(f"Failed to send WebSocket confirmation: {e}")


async def handle_websocket_confirmation_response(data: dict):
    """Handle incoming WebSocket confirmation responses from the frontend."""
    try:
        confirmation_id = data.get('confirmation_id')
        decision = data.get('decision')
        user_id = data.get('user_id')
        session_id = data.get('session_id')
        
        if not confirmation_id or not decision or not user_id:
            logger.error(f"Invalid confirmation response: {data}")
            return
            
        logger.debug(f"WEBSOCKET: Received confirmation response: user={user_id}, session={session_id}, confirmation_id={confirmation_id}, decision={decision}")
        
        # CRITICAL: Update the workflow's WebSocket context with the current active connection
        # This handles the case where the user reconnected and the workflow is still using the old connection
        if session_id:
            # Lazy import to avoid circular dependency
            from chat.backend.agent.tools.cloud_tools import update_workflow_websocket_context
            update_workflow_websocket_context(user_id, session_id) # Logging is handled by the function
        
        # Find the pending confirmation
        if confirmation_id in _pending_confirmations:
            confirmation_data = _pending_confirmations[confirmation_id]
            confirmation_data['result'] = decision
            logger.debug(f"WEBSOCKET: Confirmation {confirmation_id} resolved with decision: {decision}")
        else:
            logger.warning(f"WEBSOCKET: No pending confirmation found for ID: {confirmation_id}")
            
    except Exception as e:
        logger.error(f"Error handling WebSocket confirmation response: {e}")


def cancel_pending_confirmations_for_session(session_id: str) -> int:
    """Cancel all pending confirmations for a given session.
    
    Called when user cancels from chat input to unblock waiting confirmation threads.
    Returns the number of confirmations cancelled.
    """
    if not session_id:
        return 0
    
    cancelled_count = 0
    for confirmation_id, confirmation_data in _pending_confirmations.items():
        # Only cancel if not already resolved
        if confirmation_data.get('result') is None:
            confirmation_data['result'] = 'cancel'
            cancelled_count += 1
            logger.info(f"Cancelled pending confirmation {confirmation_id} for session {session_id}")
    
    return cancelled_count


def wait_for_user_confirmation(
    user_id: str,
    message: str,
    tool_name: str = "action",
    session_id: str = None,
    workflow_instance = None,
) -> bool:
    """
    Wait for user confirmation via WebSocket.
    Uses simple polling to check for responses.
    
    For background chats (is_background=True), confirmations are auto-approved
    since background chats run in read-only mode and don't have user interaction.
    """
    # Check if this is a background chat - auto-approve if so
    # Background chats run in read-only mode, so destructive operations should
    # already be blocked by ModeAccessController. This is an additional safety check.
    try:
        from chat.backend.agent.tools.cloud_tools import get_state_context
        state = get_state_context()
        if state and getattr(state, 'is_background', False):
            logger.info(f"[BackgroundChat] Auto-approving confirmation for {tool_name} (read-only mode)")
            return True
    except Exception as e:
        logger.debug(f"Could not check background state: {e}")
    
    # Generate a unique confirmation ID for this specific request
    timestamp_ms = int(time.time() * 1000)
    unique_id = str(uuid.uuid4())[:8]
    confirmation_id = f"{timestamp_ms}:{unique_id}"

    payload = {
        "type": "execution_confirmation",
        "data": {
            "message": message,
            "status": "awaiting_confirmation",
            "user_id": user_id,
            "confirmation_id": confirmation_id,
            "tool_name": tool_name,
            "options": [
                {"text": "Execute", "value": "execute"},
                {"text": "Cancel", "value": "cancel"},
            ],
        },
    }
    
    # Add session and user information at the top level for filtering
    if session_id:
        payload["session_id"] = session_id
    if user_id:
        payload["user_id"] = user_id

    # Save UI messages to database before waiting for confirmation
    # Get workflow instance from context if not provided
    if not workflow_instance:
        # Lazy import to avoid circular dependency
        from chat.backend.agent.tools.cloud_tools import get_workflow_context
        workflow_instance = get_workflow_context()
    
    if session_id and user_id and workflow_instance:
        logger.debug(f"Consolidating and saving UI messages before confirmation for session {session_id}")
        try:
            # First consolidate message chunks (same as workflow pattern)
            workflow_instance._consolidate_message_chunks()
            
            # Then save UI messages with confirmation
            _save_ui_messages_with_confirmation_via_workflow(workflow_instance, session_id, user_id, tool_name, message, confirmation_id)
                    
        except Exception as e:
            logger.error(f"Error consolidating or saving UI messages before confirmation: {e}")
            # Continue with confirmation even if saving fails
    elif session_id and user_id:
        logger.warning(f"No workflow instance available for UI message saving - skipping confirmation UI update")
    
    # Send confirmation prompt via WebSocket
    _send_ws(payload, tool_name)

    # Store the confirmation request (without asyncio.Event for simplicity)
    _pending_confirmations[confirmation_id] = {
        'result': None,
        'user_id': user_id,
        'timestamp': time.time()
    }

    logger.debug(f"WEBSOCKET: Waiting for user confirmation via WebSocket with ID: {confirmation_id}")

    try:
        # Simple polling approach - check every 2 seconds (can be reduced for higher responsiveness)
        elapsed = 0.0
        poll_interval = 1.0
        
        while elapsed < 300: # 5 minutes (Subjective value can be changed)
            confirmation_data = _pending_confirmations.get(confirmation_id)
            if confirmation_data and confirmation_data.get('result'):
                decision = confirmation_data['result']
                logger.debug(f"WEBSOCKET: Received decision for {confirmation_id}: {decision}")
                break
            
            time.sleep(poll_interval)
            elapsed += poll_interval
        else: # Timeout occurred
            decision = None
            logger.warning(f"WEBSOCKET: Timeout waiting for confirmation {confirmation_id}")
        
    except Exception as e:
        logger.error(f"Error waiting for confirmation: {e}")
        decision = None
    finally: # Clean up
        if confirmation_id in _pending_confirmations:
            del _pending_confirmations[confirmation_id]

    logger.debug(f"WEBSOCKET: Decision for {confirmation_id}: {decision}")
    return decision == "execute"


def _save_ui_messages_with_confirmation_via_workflow(workflow_instance, session_id: str, user_id: str, tool_name: str, message: str, confirmation_id: str) -> bool:
    """Save UI messages to database including any ongoing tool calls and confirmation marker.
    Uses workflow's methods to avoid code duplication."""
    try:
        # Get LLM messages from workflow state
        if workflow_instance._last_state:
            llm_messages = (
                workflow_instance._last_state.get('messages', [])
                if hasattr(workflow_instance._last_state, 'get')
                else getattr(workflow_instance._last_state, 'messages', [])
            )
            logger.debug(f"Found {len(llm_messages)} LLM messages in workflow state")
        else:
            logger.warning("No last state found for workflow instance")
            llm_messages = []
        
        # Get the current tool capture to include ongoing tool calls
        # Lazy import to avoid circular dependency
        from chat.backend.agent.tools.cloud_tools import get_tool_capture
        tool_capture = get_tool_capture()
        if not tool_capture or not hasattr(tool_capture, 'current_tool_calls'):
            logger.warning("No tool capture found for confirmation UI update")
            return False
        
        # Convert LLM messages to UI format using workflow's method
        ui_messages = workflow_instance._convert_to_ui_messages(llm_messages, tool_capture) # Logging is handled by the function
        
        # Find the tool call for the requesting tool and update its status
        target_tool_call = None
        for _, tool_info in tool_capture.current_tool_calls.items():
            current_tool_name = tool_info.get('tool_name')
            if current_tool_name == tool_name:
                target_tool_call = tool_info.get('call_id')  # Use call_id (call_xxx), not run_id (run-xxx)
                break
        
        if target_tool_call:
            logger.debug(f"Found target tool call: {target_tool_call} for tool {tool_name}")
            
            # Update the specific tool call to have awaiting_confirmation status
            tool_call_updated = False
            for ui_msg in ui_messages:
                if ui_msg.get('sender') == 'bot' and ui_msg.get('toolCalls'):
                    for tool_call in ui_msg.get('toolCalls', []):
                        # Match by tool_call_id (call_xxx format)
                        if tool_call.get('id') == target_tool_call:
                            # Update the tool call to show it's awaiting confirmation
                            tool_call['status'] = 'awaiting_confirmation'
                            tool_call['confirmation_id'] = confirmation_id
                            logger.debug(f"Updated tool call {target_tool_call} to awaiting_confirmation status")
                            tool_call_updated = True
                            break
                    if tool_call_updated:
                        break
            
            if not tool_call_updated:
                logger.warning(f"Could not find tool call {target_tool_call} in UI messages to update confirmation status")
        else:
            logger.warning(f"No tool call found for {tool_name} in current tool calls")
            logger.debug(f"Available tool calls: {list(tool_capture.current_tool_calls.keys())}")
        
        # Use workflow's _save_ui_messages method to save the updated messages
        logger.debug(f"Saving {len(ui_messages)} UI messages")
        return workflow_instance._save_ui_messages(session_id, user_id, ui_messages)
            
    except Exception as e:
        logger.error(f"Error saving UI messages with confirmation: {e}")
        return False