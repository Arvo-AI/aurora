"""RCA context update queue for mid-run background investigations."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage
from utils.cloud.cloud_utils import get_workflow_context
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_RCA_UPDATE_KEY_PREFIX = "rca_context_updates"
_RCA_UPDATE_TTL_SECONDS = 6 * 60 * 60  # 6 hours


def _make_update_key(user_id: str, session_id: str) -> str:
    return f"{_RCA_UPDATE_KEY_PREFIX}:{user_id}:{session_id}"


def _get_session_status(user_id: str, session_id: str) -> Optional[str]:
    """Get the current status of a chat session.
    
    Uses RLS to ensure users can only check status of their own sessions.
    """
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                # Set user context for RLS
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                
                cursor.execute(
                    "SELECT status FROM chat_sessions WHERE id = %s AND user_id = %s",
                    (session_id, user_id)
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
    """Directly append a context update to a completed session's messages in the database.
    
    For completed sessions, we insert the context update after the last bot message
    with tool calls, placing it logically at the end of the investigation.
    """
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
        
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                # Set user context for RLS
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                
                # Lock the row and get current messages to prevent race conditions
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s AND user_id = %s FOR UPDATE",
                    (session_id, user_id)
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("[RCA-UPDATE] Session %s not found for user %s", session_id, user_id)
                    return False
                
                messages = row[0] if row[0] else []
                if isinstance(messages, str):
                    try:
                        messages = json.loads(messages)
                    except json.JSONDecodeError as e:
                        logger.error(
                            "[RCA-UPDATE] Failed to parse messages JSON for session %s: %s. Using empty list.",
                            session_id, e
                        )
                        messages = []
                
                # Find the last bot message with tool calls and insert after it
                # This places the context update after the investigation tool calls
                insert_index = len(messages)
                for idx in range(len(messages) - 1, -1, -1):
                    msg = messages[idx]
                    if msg.get("sender") == "bot" and msg.get("toolCalls"):
                        insert_index = idx + 1
                        break
                
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
    session_status = _get_session_status(user_id, session_id)
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
    """Format context updates into a message that adds context without stopping investigation."""
    # Extract the key info - title and details from various payload formats
    titles = []
    details = []
    services = []
    severities = []
    sources = []
    is_manual_merge = False
    
    for update in updates:
        source = update.get("source", "unknown")
        sources.append(source)
        payload = update.get("payload", {})
        
        if isinstance(payload, dict):
            # Check if this is a manual merge with rich context
            if payload.get("merged_from_incident"):
                is_manual_merge = True
            
            # Try PagerDuty format: event.data.title
            event_data = payload.get("event", {}).get("data", {})
            title = event_data.get("title", "")
            body_details = event_data.get("body", {}).get("details", "")
            service = event_data.get("service", {}).get("summary", "")
            urgency = event_data.get("urgency", "")
            
            # Try flat format (Datadog, Grafana, manual merge, etc): title at top level
            if not title:
                title = payload.get("title", "")
            if not body_details:
                body_details = payload.get("body", "") or payload.get("message", "") or payload.get("description", "")
            if not service:
                # Try direct service field first (manual merge)
                service = payload.get("service", "")
                if not service:
                    # Datadog: extract from tags or scope
                    tags = payload.get("tags", [])
                    for tag in tags:
                        if isinstance(tag, str) and tag.startswith("service:"):
                            service = tag.split(":", 1)[1]
                            break
                    if not service:
                        service = payload.get("scope", "").replace("service:", "") if payload.get("scope", "").startswith("service:") else ""
            if not urgency:
                urgency = payload.get("alert_type", "") or payload.get("severity", "") or payload.get("status", "")
            
            if title:
                titles.append(title)
            if body_details:
                details.append(body_details)
            if service:
                services.append(service)
            if urgency:
                severities.append(urgency)
    
    title_str = titles[0] if titles else "Correlated incident"
    details_str = details[0] if details else ""
    service_str = services[0] if services else ""
    severity_str = severities[0] if severities else ""
    source_str = sources[0] if sources else "unknown"
    
    # Frame as new information to incorporate, NOT a stop signal
    parts = [
        f"**🔗 New Correlated Alert from {source_str.title()}**",
        "",
        f"**Alert:** {title_str}",
    ]
    
    if service_str:
        parts.append(f"**Service:** {service_str}")
    
    if severity_str:
        parts.append(f"**Severity:** {severity_str}")
    
    # For manual merges with rich context (investigation progress, summary), include it all
    if details_str:
        if is_manual_merge and ("## " in details_str or "Investigation progress" in details_str or "Summary" in details_str):
            # This is rich context from a manual merge - include it fully
            parts.extend([
                "",
                "**Context from merged incident:**",
                "",
                details_str,
            ])
        else:
            # Simple details
            parts.extend([
                "",
                f"**Details:** {details_str}",
            ])
    
    parts.extend([
        "",
        "This correlated alert suggests the issue may be affecting multiple services or has been detected by multiple monitoring systems. Please incorporate this information into your investigation and consider:",
        "- Whether this indicates a broader system issue",
        "- If there's a common root cause affecting multiple services",
        "- Any correlation between the timing of these alerts",
        "",
        "Continue your analysis and include this context in your final report.",
    ])

    return "\n".join(parts)


def apply_rca_context_updates(state: Any) -> Optional[HumanMessage]:
    """Inject queued updates into the in-flight RCA state as a HumanMessage.
    
    This function is called on EVERY LLM call. We check Redis for new updates
    each time, but track which updates we've already injected to avoid duplicates.
    Each new correlated alert gets injected once.
    """
    if not state:
        logger.debug("[RCA-UPDATE] No state context available")
        return None
    if not getattr(state, "is_background", False):
        logger.debug("[RCA-UPDATE] State is_background=%s, skipping", getattr(state, "is_background", None))
        return None
    if not getattr(state, "rca_context", None):
        logger.warning("[RCA-UPDATE] State has is_background=True but rca_context is None, skipping context update injection")
        return None

    user_id = getattr(state, "user_id", None)
    session_id = getattr(state, "session_id", None)
    if not user_id or not session_id:
        return None

    # Always check for new updates
    updates = drain_rca_context_updates(user_id, session_id)
    if not updates:
        logger.debug("[RCA-UPDATE] No pending updates for session %s", session_id)
        return None

    # Check if session is completed - if so, write directly to database instead of injecting into state
    session_status = _get_session_status(user_id, session_id)
    if session_status in ("completed", "failed"):
        logger.info(
            "[RCA-UPDATE] Session %s is %s, writing %d drained update(s) directly to database",
            session_id,
            session_status,
            len(updates)
        )
        # Format updates and write to database
        for update_payload in updates:
            _append_context_update_to_completed_session(user_id, session_id, update_payload)
        return None

    logger.info(
        "[RCA-UPDATE] Applying %d context update(s) for session %s",
        len(updates),
        session_id
    )

    content = _format_updates_for_prompt(updates)
    update_message = HumanMessage(content=content)

    try:
        if hasattr(state, "messages") and isinstance(state.messages, list):
            state.messages.append(update_message)
            logger.info(
                "[RCA-UPDATE] Injected context update into state.messages "
                "(session=%s, messages=%d)",
                session_id,
                len(state.messages)
            )
            
            # Store UI update payload for injection during UI conversion
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
                existing_updates = state.get("rca_ui_updates", [])
                existing_updates.append(ui_update)
                state["rca_ui_updates"] = existing_updates
            else:
                existing_updates = getattr(state, "rca_ui_updates", None) or []
                existing_updates.append(ui_update)
                setattr(state, "rca_ui_updates", existing_updates)

            workflow = get_workflow_context()
            if workflow is not None:
                wf_updates = getattr(workflow, "_rca_ui_updates", None) or []
                wf_updates.append(ui_update)
                setattr(workflow, "_rca_ui_updates", wf_updates)
    except Exception as exc:
        logger.debug("[RCA-UPDATE] Failed to append update to state messages: %s", exc)

    return update_message
