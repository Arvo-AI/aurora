from __future__ import annotations

"""Shared helper for interactive user confirmation before running destructive cloud actions.

State model:

* ``chat_sessions.messages`` — append-only history. Written only by
  ``handle_immediate_save`` (user turn) and ``_append_new_turn_ui_messages``
  (assistant/tool turn). This module never touches it.
* ``chat_sessions.pending_turn`` — ephemeral live state for an in-flight HITL
  confirmation. Set here when we prompt, cleared here when the user responds
  (or on timeout). The frontend reads it on session load and renders a
  synthetic awaiting-confirmation tool card at the tail.

This separation is what prevents the duplicate/stale-tail-card class of bugs:
there is exactly one writer per slot, no reads-then-writes, no merging.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Global store for pending confirmations - maps confirmation_id to result + metadata
_pending_confirmations: Dict[str, Dict[str, Any]] = {}

# We import send_websocket_message lazily to avoid circular imports

def _send_ws(payload: dict, tool_name: str):
    try:
        logger.debug(f"WEBSOCKET: Sending confirmation via WebSocket: {payload.get('data', {}).get('confirmation_id')} for tool {tool_name}")
        from chat.backend.agent.tools.cloud_tools import send_websocket_message
        send_websocket_message(
            payload,
            tool_name=tool_name,
            fallback_message="Awaiting user confirmation...",
        )
    except Exception as e:
        logger.error(f"Failed to send WebSocket confirmation: {e}")


def _set_pending_turn(session_id: str, user_id: str, payload: Dict[str, Any]) -> None:
    """Write the live HITL confirmation state to ``chat_sessions.pending_turn``.

    Fails open: if the DB write errors out we still fall through to the
    WebSocket prompt, because a live tab will render the confirmation via the
    WS message regardless. Only reload-into-pending requires the DB write.
    """
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            if not set_rls_context(cursor, conn, user_id, log_prefix="[PendingTurn:Set]"):
                return
            cursor.execute(
                """
                UPDATE chat_sessions
                SET pending_turn = %s, updated_at = %s
                WHERE id = %s AND user_id = %s
                """,
                (json.dumps(payload), datetime.now(), session_id, user_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to set pending_turn for session {session_id}: {e}")


def _clear_pending_turn(session_id: Optional[str], user_id: Optional[str]) -> None:
    """Clear ``chat_sessions.pending_turn``. Idempotent; safe to call twice."""
    if not session_id or not user_id:
        return
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            if not set_rls_context(cursor, conn, user_id, log_prefix="[PendingTurn:Clear]"):
                return
            cursor.execute(
                """
                UPDATE chat_sessions
                SET pending_turn = NULL, updated_at = %s
                WHERE id = %s AND user_id = %s
                """,
                (datetime.now(), session_id, user_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to clear pending_turn for session {session_id}: {e}")


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
            from chat.backend.agent.tools.cloud_tools import update_workflow_websocket_context
            update_workflow_websocket_context(user_id, session_id)

        # Resolve the waiter.
        if confirmation_id in _pending_confirmations:
            confirmation_data = _pending_confirmations[confirmation_id]
            confirmation_data['result'] = decision
            edited = data.get('edited_patterns')
            if isinstance(edited, dict):
                confirmation_data['edited_patterns'] = edited
            logger.debug(f"WEBSOCKET: Confirmation {confirmation_id} resolved with decision: {decision}")
            # Clear the durable live-state slot immediately on user response
            # so a concurrent reload in another tab sees the resolved state
            # rather than the stale prompt. The waiter's finally block also
            # clears it, which makes this a safe no-op if it ran first.
            # Only clear when the id actually matched a pending waiter --
            # otherwise a stale/late response for a superseded prompt would
            # erase the currently active slot.
            _clear_pending_turn(session_id, user_id)
        else:
            logger.warning(f"WEBSOCKET: No pending confirmation found for ID: {confirmation_id}")

    except Exception as e:
        logger.error(f"Error handling WebSocket confirmation response: {e}")


def cancel_pending_confirmations_for_session(session_id: str) -> int:
    """Cancel all pending confirmations for a given session."""
    if not session_id:
        return 0

    cancelled_count = 0
    for confirmation_id, confirmation_data in list(_pending_confirmations.items()):
        if confirmation_data.get('session_id') == session_id and confirmation_data.get('result') is None:
            confirmation_data['result'] = 'cancel'
            cancelled_count += 1
            logger.info(f"Cancelled pending confirmation {confirmation_id} for session {session_id}")

    return cancelled_count


def wait_for_user_confirmation_ex(
    *,
    user_id: str,
    message: str,
    tool_name: str,
    session_id: Optional[str],
    options: list,
    extra: Optional[Dict[str, Any]] = None,
    workflow_instance=None,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    """Extended HITL helper used exclusively by the command gate.

    Returns ``{"decision": str | None, "edited_patterns": dict}``. Persists the
    live prompt to ``chat_sessions.pending_turn`` so the UI can rehydrate the
    awaiting-confirmation card after a page reload, and always clears it on
    return (user response or timeout).
    """
    try:
        from chat.backend.agent.tools.cloud_tools import get_state_context
        state = get_state_context()
        if state and getattr(state, 'is_background', False):
            logger.warning(f"[BackgroundChat] Denying confirmation for {tool_name} -- no interactive user")
            return {"decision": "cancel", "edited_patterns": {}}
    except Exception as e:
        logger.debug(f"Could not check background state: {e}")

    timestamp_ms = int(time.time() * 1000)
    unique_id = str(uuid.uuid4())[:8]
    confirmation_id = f"{timestamp_ms}:{unique_id}"

    payload: Dict[str, Any] = {
        "type": "execution_confirmation",
        "data": {
            "message": message,
            "status": "awaiting_confirmation",
            "user_id": user_id,
            "confirmation_id": confirmation_id,
            "tool_name": tool_name,
            "options": options,
        },
    }
    if extra:
        payload["data"].update(extra)
    if session_id:
        payload["session_id"] = session_id
    if user_id:
        payload["user_id"] = user_id

    # Durable live-state slot for reload-into-pending rehydration. Mirrors
    # the WS payload's ``data`` field so the frontend can build the synthetic
    # tool card with identical semantics to the live WS path.
    if session_id:
        _set_pending_turn(session_id, user_id, {
            "confirmation_id": confirmation_id,
            "tool_name": tool_name,
            "message": message,
            "options": options,
            **(extra or {}),
            "created_at": datetime.now().isoformat(),
        })

    _pending_confirmations[confirmation_id] = {
        'result': None,
        'user_id': user_id,
        'session_id': session_id,
        'timestamp': time.time(),
    }
    # Register the waiter before sending so a fast client response can never
    # arrive before _pending_confirmations has the entry
    # (_send_ws dispatches on a daemon thread, see cloud_tools).
    _send_ws(payload, tool_name)
    logger.debug(f"WEBSOCKET: Waiting for confirmation_ex {confirmation_id}")

    decision: Optional[str] = None
    edited: Dict[str, Any] = {}
    try:
        elapsed, poll_interval = 0.0, 1.0
        while elapsed < timeout_seconds:
            data = _pending_confirmations.get(confirmation_id)
            if data and data.get('result'):
                decision = data['result']
                edited = data.get('edited_patterns') or {}
                break
            time.sleep(poll_interval)
            elapsed += poll_interval
        else:
            logger.warning(f"WEBSOCKET: Timeout waiting for confirmation_ex {confirmation_id}")
    except Exception as e:
        logger.error(f"Error waiting for confirmation_ex: {e}")
    finally:
        _pending_confirmations.pop(confirmation_id, None)
        _clear_pending_turn(session_id, user_id)

    return {"decision": decision, "edited_patterns": edited}
