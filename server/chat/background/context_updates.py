"""RCA context update queue for mid-run background investigations."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage
from utils.cloud.cloud_utils import get_workflow_context
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_RCA_UPDATE_KEY_PREFIX = "rca_context_updates"
_RCA_UPDATE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_MAX_UPDATE_CHARS = 8000


def _make_update_key(user_id: str, session_id: str) -> str:
    return f"{_RCA_UPDATE_KEY_PREFIX}:{user_id}:{session_id}"


def _get_session_status(session_id: str) -> Optional[str]:
    """Get the current status of a chat session."""
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM chat_sessions WHERE id = %s",
                    (session_id,)
                )
                row = cursor.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.warning("[RCA-UPDATE] Failed to get session status: %s", exc)
        return None


def _append_context_update_to_completed_session(
    user_id: str,
    session_id: str,
    update_payload: Dict[str, Any],
) -> bool:
    """Directly append a context update to a completed session's messages in the database."""
    try:
        from utils.db.connection_pool import db_pool
        
        content = _format_updates_for_prompt([update_payload])
        tool_call_id = f"rca_context_update_{uuid.uuid4().hex}"
        injected_at = update_payload.get("received_at") or datetime.now(timezone.utc).isoformat()
        
        # Create the context update message in UI format
        context_update_message = {
            "message_number": 0,  # Will be renumbered
            "text": "",
            "sender": "bot",
            "isCompleted": True,
            "timestamp": injected_at,
            "toolCalls": [{
                "id": tool_call_id,
                "run_id": None,
                "tool_name": "rca_context_update",
                "input": json.dumps({
                    "update_count": 1,
                    "source": update_payload.get("source", "pagerduty"),
                    "injected_at": injected_at,
                }),
                "output": content,
                "status": "completed",
                "timestamp": injected_at,
            }],
        }
        
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Get current messages
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s AND user_id = %s",
                    (session_id, user_id)
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("[RCA-UPDATE] Session %s not found for user %s", session_id, user_id)
                    return False
                
                messages = row[0] if row[0] else []
                if isinstance(messages, str):
                    messages = json.loads(messages)
                
                # Find the correct insertion position based on timestamp
                insert_index = len(messages)
                update_ts = datetime.fromisoformat(injected_at.replace("Z", "+00:00"))
                
                for idx, msg in enumerate(messages):
                    msg_ts_str = None
                    # Check toolCalls timestamps first
                    tool_calls = msg.get("toolCalls") or []
                    if tool_calls:
                        ts_values = []
                        for tc in tool_calls:
                            tc_ts = tc.get("timestamp")
                            if tc_ts:
                                try:
                                    ts_values.append(datetime.fromisoformat(tc_ts.replace("Z", "+00:00")))
                                except Exception:
                                    pass
                        if ts_values:
                            msg_ts = min(ts_values)
                            if msg_ts > update_ts:
                                insert_index = idx
                                break
                    # Fallback to message timestamp
                    elif msg.get("timestamp"):
                        try:
                            msg_ts = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
                            if msg_ts > update_ts:
                                insert_index = idx
                                break
                        except Exception:
                            pass
                
                # Insert at the correct position
                messages.insert(insert_index, context_update_message)
                
                # Renumber all messages
                for idx, msg in enumerate(messages):
                    msg["message_number"] = idx + 1
                
                # Update the database
                cursor.execute(
                    "UPDATE chat_sessions SET messages = %s, updated_at = %s WHERE id = %s AND user_id = %s",
                    (json.dumps(messages), datetime.now(), session_id, user_id)
                )
                conn.commit()
                
                logger.info(
                    "[RCA-UPDATE] Appended context update to completed session %s at position %d",
                    session_id, insert_index
                )
                return True
                
    except Exception as exc:
        logger.error("[RCA-UPDATE] Failed to append context update to completed session: %s", exc)
        return False


def enqueue_rca_context_update(
    user_id: str,
    session_id: str,
    source: str,
    payload: Dict[str, Any],
    *,
    incident_id: Optional[str] = None,
    event_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> bool:
    """Queue a correlated incident update for a background RCA session.
    
    If the session is already completed, directly append the update to the
    session's messages in the database instead of enqueueing to Redis.
    """
    if not user_id or not session_id:
        return False

    update_payload = {
        "source": source,
        "incident_id": incident_id,
        "event_id": event_id,
        "correlation_id": correlation_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }

    # Check if session is already completed
    session_status = _get_session_status(session_id)
    if session_status in ("completed", "failed"):
        logger.info(
            "[RCA-UPDATE] Session %s is %s, appending context update directly to database",
            session_id, session_status
        )
        return _append_context_update_to_completed_session(user_id, session_id, update_payload)

    # Session is still in progress - enqueue to Redis for middleware to pick up
    redis_client = get_redis_client()
    if redis_client is None:
        logger.warning("[RCA-UPDATE] Redis unavailable, skipping context update enqueue")
        return False

    try:
        key = _make_update_key(user_id, session_id)
        redis_client.rpush(key, json.dumps(update_payload))
        redis_client.expire(key, _RCA_UPDATE_TTL_SECONDS)
        logger.info(
            "[RCA-UPDATE] Enqueued context update for session %s (source=%s, incident_id=%s)",
            session_id,
            source,
            incident_id,
        )
        return True
    except Exception as exc:
        logger.warning("[RCA-UPDATE] Failed to enqueue context update: %s", exc)
        return False


def drain_rca_context_updates(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """Drain and return any queued RCA context updates for a session."""
    if not user_id or not session_id:
        return []

    redis_client = get_redis_client()
    if redis_client is None:
        return []

    key = _make_update_key(user_id, session_id)
    try:
        raw_updates = redis_client.lrange(key, 0, -1)
        if raw_updates:
            redis_client.delete(key)
        updates = []
        for raw in raw_updates or []:
            try:
                updates.append(json.loads(raw))
            except Exception:
                updates.append({"payload": raw})
        return updates
    except Exception as exc:
        logger.warning("[RCA-UPDATE] Failed to drain updates: %s", exc)
        return []


def _format_updates_for_prompt(updates: List[Dict[str, Any]]) -> str:
    parts: List[str] = [
        "CORRELATED INCIDENT CONTEXT UPDATE",
        "The following updates arrived while this RCA is running.",
        "Incorporate them into the investigation immediately.",
        "",
    ]

    for idx, update in enumerate(updates, start=1):
        payload = update.get("payload")
        try:
            payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            payload_str = str(payload)
        if len(payload_str) > _MAX_UPDATE_CHARS:
            payload_str = payload_str[:_MAX_UPDATE_CHARS] + "\n... [truncated]"

        parts.extend([
            f"Update {idx}:",
            f"- Source: {update.get('source')}",
            f"- Incident ID: {update.get('incident_id')}",
            f"- Event ID: {update.get('event_id')}",
            f"- Correlation ID: {update.get('correlation_id')}",
            f"- Received At: {update.get('received_at')}",
            "Payload:",
            payload_str,
            "",
        ])

    return "\n".join(parts).strip()


def apply_rca_context_updates(state: Any) -> Optional[SystemMessage]:
    """Inject queued updates into the in-flight RCA state (system message)."""
    if not state or not getattr(state, "is_background", False):
        return None
    if not getattr(state, "rca_context", None):
        return None

    user_id = getattr(state, "user_id", None)
    session_id = getattr(state, "session_id", None)
    if not user_id or not session_id:
        return None

    updates = drain_rca_context_updates(user_id, session_id)
    if not updates:
        return None

    content = _format_updates_for_prompt(updates)
    update_message = SystemMessage(content=content)

    try:
        if hasattr(state, "messages") and isinstance(state.messages, list):
            state.messages.append(update_message)
            # Store a UI update payload to be injected into tool calls during UI conversion.
            # This avoids forcing a new message to appear at the top of the tool call list.
            tool_call_id = f"rca_context_update_{uuid.uuid4().hex}"
            injected_at = updates[0].get("received_at") if updates else None
            ui_update = {
                "tool_call_id": tool_call_id,
                "content": content,
                "injected_at": injected_at,
                "update_count": len(updates),
                "source": "pagerduty" if len(updates) == 1 else "multiple",
            }
            if isinstance(state, dict):
                existing_updates = state.get("rca_ui_updates")
                if not isinstance(existing_updates, list):
                    existing_updates = []
                existing_updates.append(ui_update)
                state["rca_ui_updates"] = existing_updates
            else:
                existing_updates = getattr(state, "rca_ui_updates", None)
                if not isinstance(existing_updates, list):
                    existing_updates = []
                existing_updates.append(ui_update)
                setattr(state, "rca_ui_updates", existing_updates)

            workflow = get_workflow_context()
            if workflow is not None:
                wf_updates = getattr(workflow, "_rca_ui_updates", None)
                if not isinstance(wf_updates, list):
                    wf_updates = []
                wf_updates.append(ui_update)
                setattr(workflow, "_rca_ui_updates", wf_updates)
    except Exception as exc:
        logger.debug("[RCA-UPDATE] Failed to append update to state messages: %s", exc)

    return update_message
