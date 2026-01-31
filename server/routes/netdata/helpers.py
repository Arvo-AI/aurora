"""Helper functions for Netdata integration."""

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def safe_str(value: Any) -> Optional[str]:
    """Convert value to string, handling dict/list types."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def normalize_netdata_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Netdata payload (v1 and v2) into a flat structure.
    
    Handles the difference between flat v1 payloads and nested v2 payloads
    where alert details are under an 'alert' key.
    """
    alert_obj = payload.get("alert") or {}
    node_obj = payload.get("node") or {}
    alert_state = alert_obj.get("state") or {}
    chart_obj = alert_obj.get("chart") or {}
    space_obj = payload.get("space") or {}
    rendered_obj = alert_obj.get("rendered") or {}
    duration_obj = alert_obj.get("duration") or {}

    # Extract fields with v2 (nested) taking precedence, falling back to v1 (flat)
    data = {
        "name": (payload.get("alarm") or 
                 payload.get("title") or 
                 payload.get("alert_name") or
                 alert_obj.get("name")),
        
        "status": (payload.get("status") or 
                   alert_state.get("status") or 
                   ("test" if payload.get("title") == "Test Notification" else None)),
                   
        "class": payload.get("class") or alert_obj.get("config", {}).get("classification"),
        "family": payload.get("family"),
        "chart": payload.get("chart") or chart_obj.get("name") or chart_obj.get("id"),
        "host": payload.get("host") or node_obj.get("hostname"),
        "space": payload.get("space") or space_obj.get("name") or space_obj.get("slug"),
        "room": payload.get("room"),
        "value": payload.get("value") or alert_state.get("value") or alert_state.get("value_str"),
        "message": (payload.get("message") or 
                    payload.get("info") or 
                    rendered_obj.get("info") or 
                    rendered_obj.get("summary")),
        
        # Additional metadata fields
        "context": payload.get("context") or alert_obj.get("context"),
        "duration": payload.get("duration") or duration_obj.get("value_str") or duration_obj.get("value"),
        "alert_url": payload.get("alert_url") or alert_obj.get("url"),
        
        # Raw counters (pass through)
        "additional_critical": payload.get("additional_active_critical_alerts"),
        "additional_warning": payload.get("additional_active_warning_alerts")
    }

    # Apply safe string conversion to all fields except the counters
    for k, v in data.items():
        if k not in ("additional_critical", "additional_warning"):
            data[k] = safe_str(v)

    return data


def format_alert_summary(normalized: Dict[str, Any]) -> str:
    """Format alert summary for logging."""
    alarm = normalized.get("name") or "Unnamed"
    status = normalized.get("status") or "unknown"
    host = normalized.get("host") or "unknown"
    return f"{alarm} [{status}] on {host}"


def generate_alert_hash(user_id: str, normalized: Dict[str, Any], received_at: datetime) -> str:
    """Generate a unique hash for deduplication."""
    key_data = f"{user_id}:{normalized.get('name') or ''}:{normalized.get('host') or ''}:{normalized.get('status') or ''}:{received_at.isoformat()}"
    return hashlib.sha256(key_data.encode()).hexdigest()[:64]


def should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background chat should be triggered for this alert.
    
    Args:
        user_id: The user ID receiving the alert
        payload: The Netdata alert payload
    
    Returns:
        True if a background chat should be triggered
    """
    # Check user preference for automated RCA
    # from utils.auth.stateless_auth import get_user_preference
    # rca_enabled = get_user_preference(user_id, "automated_rca_enabled", default=False)
    # 
    # if not rca_enabled:
    #     logger.debug("[NETDATA] Skipping background RCA - disabled in user preferences for user %s", user_id)
    #     return False
    
    # Always trigger RCA for any webhook received
    return True


def build_rca_prompt_from_alert(normalized: Dict[str, Any], user_id: Optional[str] = None) -> str:
    """Build an RCA analysis prompt from normalized Netdata alert data.

    Args:
        normalized: The normalized Netdata alert data (from normalize_netdata_payload)
        user_id: Optional user ID for Aurora Learn context injection

    Returns:
        A prompt string for the background chat agent
    """
    alarm = normalized.get("name") or "Unknown Alert"
    status = normalized.get("status") or "unknown"
    host = normalized.get("host") or "unknown"
    chart = normalized.get("chart") or "unknown"
    alert_class = normalized.get("class") or "unknown"
    family = normalized.get("family") or "unknown"
    space = normalized.get("space") or "unknown"
    room = normalized.get("room") or "unknown"
    value = normalized.get("value")
    message = normalized.get("message") or ""

    # Build the prompt parts
    prompt_parts = [
        "A Netdata alert has been triggered and requires Root Cause Analysis.",
        "",
        "ALERT DETAILS:",
        f"- Alarm: {alarm}",
        f"- Status: {status}",
        f"- Host: {host}",
        f"- Chart: {chart}",
        f"- Class: {alert_class}",
        f"- Family: {family}",
        f"- Space: {space}",
        f"- Room: {room}",
    ]

    if value:
        prompt_parts.append(f"- Value: {value}")

    if message:
        prompt_parts.append(f"- Message: {message}")

    # Add Aurora Learn context if available
    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context
        inject_aurora_learn_context(prompt_parts, user_id, alarm, host, "netdata")
    except Exception as e:
        logger.warning(f"[AURORA LEARN] Failed to get context: {e}")

    return "\n".join(prompt_parts)
