"""
RCA (Root Cause Analysis) prompt builder for background alert processing.

build_rca_prompt() is the single entry point for all RCA prompt
construction — both webhook-triggered and user-initiated (chat) RCAs.
It passes the raw payload directly to the LLM, with conditional truncation
for large payloads.

The agent has access to org memory (learned entries from past RCAs, infrastructure
topology, runbooks) via its memory tools — no explicit injection needed here.
"""

from typing import Any, Dict, List, Optional
import json
import logging

logger = logging.getLogger(__name__)


def build_alert_rail_text(alert_details: Dict[str, Any]) -> str:
    """Extract the webhook-authored subset of an alert for input-rail evaluation.

    Synthesized RCA prompts wrap externally-controlled fields (alert title,
    status, message/description) in a large instruction scaffold. The scaffold
    is not user input and must not be fed to the prompt-injection rail (it
    produces false positives with stricter models). This helper returns only
    the webhook-provided text so the rail evaluates exactly the attacker-
    controllable surface.
    """
    parts: List[str] = []
    title = alert_details.get('title')
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    status = alert_details.get('status')
    if isinstance(status, str) and status.strip() and status.strip().lower() != 'unknown':
        parts.append(f"Status: {status.strip()}")
    message = alert_details.get('message')
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    return "\n\n".join(parts)


def get_user_providers(user_id: str) -> List[str]:
    """Return verified providers for a user.

    Single source of truth: cloud providers (aws/gcp/azure/ovh/scaleway)
    come from user_connections (role-based auth, always valid).
    Integration providers come from SkillRegistry connection checks
    (credential-validated). The agent never sees providers it can't use.
    """
    if not user_id:
        return []

    _cloud_providers = {'aws', 'gcp', 'azure', 'ovh', 'scaleway'}
    verified = []

    try:
        from utils.auth.stateless_auth import get_connected_providers
        all_db = get_connected_providers(user_id)
        verified = [p for p in all_db if p.lower() in _cloud_providers]
    except Exception as e:
        logger.warning(f"Error fetching cloud providers: {e}")

    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        registry = SkillRegistry.get_instance()
        connected_skill_ids = registry.get_connected_skill_ids(user_id)
        verified.extend(connected_skill_ids)
    except Exception as e:
        logger.warning(f"Error fetching connected skills: {e}")

    result = sorted(set(verified))
    logger.info(f"Verified providers for user {user_id}: {result}")
    return result


# ============================================================================
# Unified Raw Payload RCA Prompt Builder
# ============================================================================

PAYLOAD_CHAR_THRESHOLD = 1_000
CHAT_PAYLOAD_MAX =60_000

def _extract_rail_text_from_payload(payload: Dict[str, Any]) -> str:
    """Extract attacker-controllable text from a raw payload for guardrail evaluation."""
    _RAIL_FIELDS = {
        'title', 'message', 'body', 'description', 'text', 'summary',
        'alert_title', 'event_title', 'rulename', 'name', 'condition_name',
    }
    parts: List[str] = []

    def _collect(obj: Any, depth: int = 0) -> None:
        if depth > 2:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(val, str) and key.lower().rstrip('_') in _RAIL_FIELDS:
                    stripped = val.strip()
                    if stripped:
                        parts.append(stripped)
                elif isinstance(val, (dict, list)):
                    _collect(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:5]:
                _collect(item, depth + 1)

    _collect(payload)
    combined = "\n\n".join(parts)
    return combined[:3000]


def build_rca_prompt(
    source: str,
    title: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> tuple[str, str]:
    """Build an RCA prompt by passing the raw payload directly to the LLM.

    Instead of manually extracting fields, we pass the raw JSON so the LLM
    parses it directly. Payloads under PAYLOAD_CHAR_THRESHOLD are passed
    verbatim; larger ones get per-field truncation so the agent can drill
    down via the get_alert_field tool.

    Args:
        source: Provider name (grafana, datadog, incidentio, chat, etc.)
        title: Alert title (already extracted by the caller for incident creation)
        payload: The raw webhook payload dict (or synthetic payload for chat RCAs)
        user_id: For provider lookup, Aurora Learn, and prediscovery context

    Returns:
        (prompt, rail_text) tuple
    """
    from chat.backend.agent.tools.output_sanitizer import truncate_json_fields

    providers = get_user_providers(user_id) if user_id else []

    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        payload_size = len(serialized)

        if source == "chat":
            if payload_size > CHAT_PAYLOAD_MAX:
                json_content = serialized[:CHAT_PAYLOAD_MAX] + "\n... [message truncated]"
            else:
                json_content = serialized
            truncation_note = ""
        elif payload_size <= PAYLOAD_CHAR_THRESHOLD:
            json_content = serialized
            truncation_note = ""
        else:
            truncated = truncate_json_fields(payload, max_field_length=250)
            json_content = json.dumps(truncated, ensure_ascii=False, default=str)
            if len(json_content) > 15_000:
                truncated = truncate_json_fields(payload, max_field_length=80, max_depth=1)
                json_content = json.dumps(truncated, indent=2, ensure_ascii=False, default=str)
            truncation_note = (
                "Fields ending with '... [field truncated]' were too long to include in full. "
                "`get_alert_field` tool for fields that show this marker if you need to inspect them. "
            )
    except Exception as e:
        logger.warning(f"Failed to serialize alert payload: {e}")
        json_content = f"[Payload could not be serialized — use get_alert_field to inspect fields. Keys: {list(payload.keys())[:20]}]"
        truncation_note = ""

    prompt_parts = [
        f"# ROOT CAUSE ANALYSIS REQUIRED - {source.upper()} ALERT",
        "",
        f"## ALERT: {title}",
        "",
        "## CONNECTED INFRASTRUCTURE:",
        f"You have access to: {', '.join(providers) if providers else 'No cloud/monitoring providers connected'}",
        "",
        "## WEBHOOK PAYLOAD:",
        truncation_note + "<alert_payload>",
        json_content,
        "</alert_payload>",
    ]

    prompt = "\n".join(prompt_parts)
    rail_text = _extract_rail_text_from_payload(payload)

    return prompt, rail_text
