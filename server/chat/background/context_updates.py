"""RCA context update queue for mid-run background investigations."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_RCA_UPDATE_KEY_PREFIX = "rca_context_updates"
_RCA_UPDATE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_MAX_UPDATE_CHARS = 8000


def _make_update_key(user_id: str, session_id: str) -> str:
    return f"{_RCA_UPDATE_KEY_PREFIX}:{user_id}:{session_id}"


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
    """Queue a correlated incident update for a background RCA session."""
    if not user_id or not session_id:
        return False

    redis_client = get_redis_client()
    if redis_client is None:
        logger.warning("[RCA-UPDATE] Redis unavailable, skipping context update enqueue")
        return False

    update_payload = {
        "source": source,
        "incident_id": incident_id,
        "event_id": event_id,
        "correlation_id": correlation_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }

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
            # Also append a visible tool-call style message for UI history.
            # This makes the update obvious in chat_sessions.messages after completion.
            tool_call_id = f"rca_context_update_{uuid.uuid4().hex}"
            injected_at = updates[0].get("received_at") if updates else None
            tool_args = {
                "update_count": len(updates),
                "source": "pagerduty" if len(updates) == 1 else "multiple",
                "injected_at": injected_at,
            }
            tool_call_msg = AIMessage(
                content="Received correlated incident context update.",
                additional_kwargs={
                    "timestamp": injected_at,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "function": {
                                "name": "rca_context_update",
                                "arguments": json.dumps(tool_args),
                            },
                            "type": "function",
                        }
                    ]
                },
            )
            tool_result_msg = ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
            )
            state.messages.append(tool_call_msg)
            state.messages.append(tool_result_msg)
    except Exception as exc:
        logger.debug("[RCA-UPDATE] Failed to append update to state messages: %s", exc)

    return update_message
