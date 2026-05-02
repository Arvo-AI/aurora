from __future__ import annotations

"""Shared helper for interactive user confirmation before running destructive cloud actions."""

import logging
import time
import uuid
from typing import Any, Dict, Optional
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


def resolve_confirmation(
    confirmation_id: str,
    decision: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Transport-neutral confirmation resolver.

    Routes a user's approve/decline answer into the in-process pending-confirmation
    map that ``wait_for_user_confirmation`` polls. Returns True if the
    confirmation was found and resolved, False otherwise.

    Idempotent: a duplicate decision for an already-resolved confirmation_id
    is logged at INFO and dropped.
    """
    if not confirmation_id or not decision:
        logger.error(
            "resolve_confirmation: missing confirmation_id or decision (cid=%s decision=%s)",
            confirmation_id, decision,
        )
        return False

    # Refresh the workflow's WS context if we have a session_id and a WS-bound
    # workflow exists. No-op for SSE-only sessions.
    if session_id and user_id:
        try:
            from chat.backend.agent.tools.cloud_tools import update_workflow_websocket_context
            update_workflow_websocket_context(user_id, session_id)
        except Exception as e:
            logger.debug(f"update_workflow_websocket_context skipped: {e}")

    confirmation_data = _pending_confirmations.get(confirmation_id)
    if confirmation_data is None:
        logger.warning(
            "resolve_confirmation: no pending confirmation for ID: %s",
            confirmation_id,
        )
        return False

    if confirmation_data.get('result') is not None:
        logger.info(
            "resolve_confirmation: confirmation %s already resolved (result=%s); dropping duplicate",
            confirmation_id, confirmation_data.get('result'),
        )
        return False

    confirmation_data['result'] = decision
    logger.debug(
        "resolve_confirmation: %s resolved with decision: %s",
        confirmation_id, decision,
    )
    return True


# SSE → WS decision mapping. The WS path uses execute/cancel (paired with the
# UI button values); the SSE POST uses approve/decline. Normalize at the edge.
_SSE_DECISION_MAP = {"approve": "execute", "decline": "cancel"}


def normalize_decision(raw: Optional[str]) -> Optional[str]:
    """Map either WS or SSE decision strings onto the canonical execute/cancel
    values that ``wait_for_user_confirmation`` checks. Returns None on bad input."""
    if not raw:
        return None
    raw = str(raw).strip().lower()
    if raw in {"execute", "cancel"}:
        return raw
    return _SSE_DECISION_MAP.get(raw)


async def handle_websocket_confirmation_response(data: dict):
    """WS-side adapter that unpacks the dict payload and delegates to
    ``resolve_confirmation``. Kept so the WS path in main_chatbot.py is
    unchanged."""
    try:
        confirmation_id = data.get('confirmation_id')
        decision = normalize_decision(data.get('decision'))
        user_id = data.get('user_id')
        session_id = data.get('session_id')

        if not confirmation_id or not decision or not user_id:
            logger.error(f"Invalid confirmation response: {data}")
            return

        logger.debug(
            "WEBSOCKET: Received confirmation response: user=%s, session=%s, confirmation_id=%s, decision=%s",
            user_id, session_id, confirmation_id, decision,
        )

        resolve_confirmation(
            confirmation_id=confirmation_id,
            decision=decision,
            user_id=user_id,
            session_id=session_id,
        )
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
        if confirmation_data.get('session_id') != session_id:
            continue
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
    
    For background chats (is_background=True), confirmations are denied
    since there is no interactive user to approve destructive operations.
    """
    # Background chats have no user interaction channel, so destructive operations
    # must be denied rather than auto-approved.
    try:
        from chat.backend.agent.tools.cloud_tools import get_state_context
        state = get_state_context()
        if state and getattr(state, 'is_background', False):
            logger.warning(f"[BackgroundChat] Denying confirmation for {tool_name} -- no interactive user")
            return False
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
    
    _send_ws(payload, tool_name)

    try:
        tool_call_id = None
        from chat.backend.agent.tools.cloud_tools import get_tool_capture
        tool_capture = get_tool_capture()
        if tool_capture is not None and hasattr(tool_capture, 'current_tool_calls'):
            for _, tool_info in tool_capture.current_tool_calls.items():
                if tool_info.get('tool_name') == tool_name:
                    tool_call_id = tool_info.get('call_id')
                    break

        sse_payload = dict(payload["data"])
        if tool_call_id:
            sse_payload["tool_call_id"] = tool_call_id

        from chat.backend.agent.tools.cloud_tools import _emit_event
        logger.info(
            "[infra_confirm] emitting execution_confirmation cid=%s tool=%s tool_call_id=%s session=%s",
            confirmation_id, tool_name, tool_call_id, session_id,
        )
        _emit_event("execution_confirmation", sse_payload)
    except Exception as e:
        logger.error(f"Failed to record execution_confirmation chat_event: {e}")

    # session_id is for cancel_pending_confirmations_for_session to filter on.
    _pending_confirmations[confirmation_id] = {
        'result': None,
        'user_id': user_id,
        'session_id': session_id,
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


def _merge_confirmation_messages(existing: list, ui_messages: list, target_tool_call_id: Optional[str]) -> list:
    """Merge the awaiting-confirmation snapshot with existing chat_sessions.messages.
    Preserves user bubbles; updates the matching tool_call in place or appends new bots.
    """
    if not ui_messages:
        return existing or []
    if not isinstance(existing, list) or not existing:
        return ui_messages

    if target_tool_call_id:
        for msg in existing:
            if msg.get('sender') != 'bot':
                continue
            for tc in (msg.get('toolCalls') or []):
                if tc.get('id') == target_tool_call_id:
                    new_tc = _find_tool_call(ui_messages, target_tool_call_id)
                    if new_tc:
                        tc.update(new_tc)
                    return existing

    last_existing = existing[-1] if existing else None
    new_tail = ui_messages[-1] if ui_messages else None
    if (
        last_existing and new_tail
        and last_existing.get('sender') == 'bot'
        and new_tail.get('sender') == 'bot'
    ):
        existing[-1] = new_tail
        return existing

    return existing + [m for m in ui_messages if m.get('sender') == 'bot']


def _find_tool_call(ui_messages: list, tool_call_id: str) -> Optional[dict]:
    for msg in ui_messages:
        for tc in (msg.get('toolCalls') or []):
            if tc.get('id') == tool_call_id:
                return tc
    return None


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
        
        # Merge instead of overwrite — handle_immediate_save persisted the user
        # bubble; overwriting with [bot] alone would wipe it on mid-confirm reload.
        # If the merge cannot complete (DB error, RLS miss), abort the save so we
        # don't clobber the existing chat_sessions.messages with un-merged data.
        try:
            from utils.db.connection_pool import db_pool
            from utils.auth.stateless_auth import set_rls_context
            existing: list = []
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                if set_rls_context(cursor, conn, user_id, log_prefix="[Workflow:ConfMerge]"):
                    cursor.execute(
                        "SELECT messages FROM chat_sessions WHERE id = %s AND user_id = %s",
                        (session_id, user_id),
                    )
                    row = cursor.fetchone()
                    if row and isinstance(row[0], list):
                        existing = row[0]
            ui_messages = _merge_confirmation_messages(existing, ui_messages, target_tool_call)
        except Exception as e:
            logger.error(
                f"Failed to merge with existing chat_sessions.messages, aborting save: {e}"
            )
            return False

        logger.debug(f"Saving {len(ui_messages)} UI messages (chat_events suppressed for confirmation pause)")
        return workflow_instance._save_ui_messages(
            session_id, user_id, ui_messages, emit_chat_events=False
        )
            
    except Exception as e:
        logger.error(f"Error saving UI messages with confirmation: {e}")
        return False