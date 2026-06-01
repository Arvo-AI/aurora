"""
RCA (Root Cause Analysis) prompt builder for background alert processing.

build_rca_prompt() is the single entry point for all RCA prompt
construction — both webhook-triggered and user-initiated (chat) RCAs.
It passes the raw payload directly to the LLM, with conditional truncation
for large payloads.

Aurora Learn Integration:
- When Aurora Learn is enabled, searches for similar past incidents with positive feedback
- Injects context from helpful RCAs to improve new investigations
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


# ============================================================================
# Aurora Learn - Similar RCA Context Injection
# ============================================================================


def _is_aurora_learn_enabled(user_id: str) -> bool:
    """Check if Aurora Learn is enabled for a user. Defaults to True."""
    if not user_id:
        return False
    try:
        from utils.auth.stateless_auth import get_user_preference
        setting = get_user_preference(user_id, "aurora_learn_enabled", default=True)
        return setting is True
    except Exception as e:
        logger.warning(f"Error checking Aurora Learn setting: {e}")
        return True  # Default to enabled


def inject_aurora_learn_context(
    prompt_parts: list,
    user_id: Optional[str],
    alert_title: str,
    alert_service: str,
    source_type: str,
) -> None:
    """
    Append Aurora Learn context to prompt_parts if similar RCAs are found.

    This is a convenience wrapper for connector modules to inject Aurora Learn
    context into their RCA prompts without duplicating the try/except pattern.

    Args:
        prompt_parts: List of prompt strings to append to (modified in place)
        user_id: User ID for Aurora Learn lookup
        alert_title: Title of the alert
        alert_service: Service associated with the alert
        source_type: Source type (grafana, datadog, etc.)
    """
    if not user_id:
        return

    similar_context = _get_similar_good_rcas_context(
        user_id=user_id,
        alert_title=alert_title,
        alert_service=alert_service,
        source_type=source_type,
    )
    if similar_context:
        prompt_parts.append(similar_context)


def _get_similar_good_rcas_context(
    user_id: str,
    alert_title: str,
    alert_service: str,
    source_type: str,
) -> str:
    """
    Check if Aurora Learn is enabled and search for similar good RCAs.

    Returns formatted context string if matches found, empty string otherwise.
    """
    if not user_id:
        return ""

    # Check if Aurora Learn is enabled
    if not _is_aurora_learn_enabled(user_id):
        logger.debug(f"Aurora Learn disabled for user {user_id}, skipping context injection")
        return ""

    try:
        from routes.incident_feedback.weaviate_client import search_similar_good_rcas

        # Search for similar incidents with positive feedback
        matches = search_similar_good_rcas(
            user_id=user_id,
            alert_title=alert_title,
            alert_service=alert_service,
            source_type=source_type,
            limit=2,
            min_score=0.7,
        )

        if not matches:
            logger.debug(f"No similar good RCAs found for alert: {alert_title[:50]}...")
            return ""

        # Format matches for injection
        context_parts = [
            "",
            "## CONTEXT FROM SIMILAR PAST INCIDENTS:",
            "The following past RCAs were rated helpful by the user. Use this context to guide your investigation:",
            "",
        ]

        for i, match in enumerate(matches, 1):
            similarity_pct = int(match["similarity"] * 100)
            context_parts.extend([
                f"### Past Incident {i} (Similarity: {similarity_pct}%)",
                f"- **Alert**: {match.get('alert_title', 'Unknown')}",
                f"- **Service**: {match.get('alert_service', 'Unknown')}",
                f"- **Source**: {match.get('source_type', 'Unknown')}",
                "",
                "**Summary of what was found:**",
                match.get("aurora_summary", "No summary available")[:1000],  # Limit length
                "",
            ])

            # Add key investigation steps from thoughts (summarized)
            thoughts = match.get("thoughts", [])
            if thoughts:
                # Get the most relevant thoughts (findings and actions)
                key_thoughts = [
                    t["content"]
                    for t in thoughts
                    if t.get("type") in ("finding", "action", "hypothesis", "analysis")
                ][:3]
                if key_thoughts:
                    context_parts.append("**Key investigation steps:**")
                    for thought in key_thoughts:
                        # Truncate long thoughts
                        truncated = thought[:200] + "..." if len(thought) > 200 else thought
                        context_parts.append(f"- {truncated}")
                    context_parts.append("")

            # Add commands used during investigation (without outputs)
            citations = match.get("citations", [])
            if citations:
                commands = [
                    c.get("command", "")
                    for c in citations
                    if c.get("command")
                ][:5]
                if commands:
                    context_parts.append("**Commands used in investigation:**")
                    for cmd in commands:
                        truncated = cmd[:150] + "..." if len(cmd) > 150 else cmd
                        context_parts.append(f"- `{truncated}`")
                    context_parts.append("")

        context_parts.extend([
            "---",
            "**Note**: Use the above context as guidance. The current incident may have different root causes.",
            "",
        ])

        context = "\n".join(context_parts)
        logger.info(
            f"[AURORA LEARN] Injecting context from {len(matches)} similar good RCAs for user {user_id}"
        )
        logger.info(f"[AURORA LEARN] Context preview:\n{context[:500]}...")
        return context

    except Exception as e:
        logger.warning(f"Error getting similar RCA context: {e}")
        return ""


def _get_prediscovery_context(user_id: str, alert_title: str, alert_service: str) -> str:
    """Search prediscovery findings relevant to the alert and return formatted context."""
    if not user_id:
        return ""

    query = " ".join(filter(None, [alert_title, alert_service]))
    if not query.strip():
        return ""

    try:
        from routes.knowledge_base.weaviate_client import _get_weaviate_client
        from weaviate.classes.query import Filter, HybridFusion
        from utils.auth.stateless_auth import get_org_id_for_user

        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return ""

        _, collection = _get_weaviate_client()

        discovery_filter = (
            Filter.by_property("org_id").equal(org_id)
            & Filter.by_property("document_id").like("discovery:*")
        )

        response = collection.query.hybrid(
            query=query,
            limit=3,
            alpha=0.5,
            fusion_type=HybridFusion.RANKED,
            filters=discovery_filter,
            return_metadata=["score"],
        )

        if not response.objects:
            return ""

        parts = [
            "",
            "## INFRASTRUCTURE TOPOLOGY CONTEXT (from pre-discovery):",
            "The following infrastructure mappings were discovered automatically and may be relevant:",
            "",
        ]

        for obj in response.objects:
            source = obj.properties.get("source_filename", "")
            content = obj.properties.get("content", "")
            if content:
                label = source.replace("[Auto-Discovery] ", "") if source else "Discovery"
                parts.append(f"### {label}")
                parts.append(content[:2000])
                parts.append("")

        parts.append("Use this topology context to understand dependencies and blast radius.")
        parts.append("")

        context = "\n".join(parts)
        logger.info(f"[PREDISCOVERY] Injected {len(response.objects)} findings for alert: {query[:50]}")
        return context

    except Exception as e:
        logger.warning(f"Error getting prediscovery context: {e}")
        return ""


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

    # Aurora Learn: inject context from similar past incidents
    metadata = payload.get("metadata")
    metadata_service = metadata.get("service", "") if isinstance(metadata, dict) else ""
    alert_service = (
        payload.get("service")
        or payload.get("resource")
        or payload.get("component")
        or metadata_service
        or ""
    )
    if user_id:
        similar_context = _get_similar_good_rcas_context(
            user_id=user_id,
            alert_title=title,
            alert_service=alert_service,
            source_type=source,
        )
        if similar_context:
            prompt_parts.append(similar_context)

    # Prediscovery: inject infrastructure topology context
    if user_id:
        prediscovery_context = _get_prediscovery_context(
            user_id=user_id,
            alert_title=title,
            alert_service=alert_service,
        )
        if prediscovery_context:
            prompt_parts.append(prediscovery_context)

    prompt = "\n".join(prompt_parts)
    rail_text = _extract_rail_text_from_payload(payload)

    return prompt, rail_text
