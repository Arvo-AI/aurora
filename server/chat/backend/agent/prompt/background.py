from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from chat.backend.agent.skills.loader import (
    load_core_prompt,
    resolve_template,
    CORE_SKILLS_DIR,
    BACKGROUND_RCA_DIR,
)

logger = logging.getLogger(__name__)


def _append_segment(
    parts: List[str],
    segment_name: str,
    base_dir: str = BACKGROUND_RCA_DIR,
    context: Optional[Dict[str, Any]] = None,
    leading_blank: bool = False,
    trailing_blank: bool = False,
) -> None:
    """Load a segment .md file and append it to the prompt parts list."""
    try:
        content = load_core_prompt(base_dir, segments=[segment_name]).strip()
    except Exception as e:
        logger.warning(f"Failed to load segment '{segment_name}': {e}")
        return

    if not content:
        return

    if context:
        try:
            content = resolve_template(content, context)
        except Exception:
            pass

    if leading_blank:
        parts.append("")
    parts.append(content)
    if trailing_blank:
        parts.append("")


def build_background_mode_segment(state: Optional[Any]) -> str:
    """Build background mode instructions for RCA or prediscovery chats."""
    if not state:
        return ""

    if not getattr(state, 'is_background', False):
        return ""

    rca_context = getattr(state, 'rca_context', None)
    if not rca_context:
        return ""

    source = rca_context.get('source', '').lower()
    providers = rca_context.get('providers', [])
    integrations = rca_context.get('integrations', {})

    source_display = "USER-REPORTED INCIDENT" if source == "chat" else f"{source.upper()} alert"
    providers_display = ", ".join(providers) if providers else "None"
    providers_tools_display = ", ".join(providers) if providers else "none"

    parts: List[str] = []
    _append_segment(
        parts,
        "background_header",
        context={
            "source_display": source_display,
            "providers_display": providers_display,
        },
        trailing_blank=True,
    )
    _append_segment(
        parts,
        "background_provider_tools",
        context={"providers_tools_display": providers_tools_display},
        leading_blank=True,
    )

    # Load integration-specific RCA guidance from skill files
    user_id = rca_context.get('user_id', '')
    if user_id:
        try:
            from chat.backend.agent.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            rca_skills_content = registry.load_skills_for_rca(
                user_id=user_id,
                source=source,
                providers=providers,
                integrations=integrations,
                alert_details=rca_context.get('trigger_metadata', {}),
            )
            if rca_skills_content:
                parts.extend(["", rca_skills_content])
        except Exception as e:
            logger.warning(f"Failed to load RCA skills: {e}")
    else:
        logger.warning("Skipping RCA skill loading — user_id missing from rca_context")

    # Integration-specific guidance (Splunk, Datadog, GitHub, Jira, etc.)
    # now loaded from skill files above via SkillRegistry.load_skills_for_rca().

    # Memory behavioral contract — shared with foreground chat (single source of truth)
    _append_segment(parts, "memory", leading_blank=True, base_dir=CORE_SKILLS_DIR)
    _append_segment(parts, "background_vm_access", leading_blank=True)
    _append_segment(
        parts,
        "background_context_update",
        leading_blank=True,
        trailing_blank=True,
    )

    # Critical requirements - MUST complete all before stopping
    if source == 'slack':
        _append_segment(parts, "background_source_slack", leading_blank=True)
    elif source == 'google_chat':
        _append_segment(parts, "background_source_google_chat", leading_blank=True)
    else:
        _append_segment(
            parts,
            "background_source_general",
            context={"providers_display": providers_display},
            leading_blank=True,
        )

        # Non-Anthropic models often don't produce text between tool calls unless instructed to
        model_name = (getattr(state, 'model', '') or '').lower()
        if model_name and not model_name.startswith("anthropic/"):
            _append_segment(parts, "background_source_general_non_anthropic")

        _append_segment(parts, "background_source_general_footer")

    return "\n".join(parts)


def build_action_mode_segment(state: Optional[Any]) -> str:
    """Build action mode segment: eager-loaded skills, no RCA mandates."""
    if not state:
        return ""

    rca_context = getattr(state, 'rca_context', None)
    if not rca_context:
        return ""

    providers = rca_context.get('providers', [])
    integrations = rca_context.get('integrations', {})
    user_id = rca_context.get('user_id', '')

    parts: List[str] = [
        "INTEGRATIONS PRE-LOADED — do NOT call load_skill(), skills are already available.",
    ]

    if user_id:
        try:
            from chat.backend.agent.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            skills_content = registry.load_skills_for_rca(
                user_id=user_id,
                source='action',
                providers=providers,
                integrations=integrations,
            )
            if skills_content:
                parts.append(skills_content)
        except Exception as e:
            logger.warning("Failed to load skills for action: %s", e)

    return "\n\n".join(parts)
