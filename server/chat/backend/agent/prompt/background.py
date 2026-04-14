from __future__ import annotations

from functools import lru_cache
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BACKGROUND_RCA_SEGMENTS_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "skills",
        "rca",
        "background",
    )
)


@lru_cache(maxsize=32)
def _load_background_segment_template(segment_name: str) -> str:
    """Load a background RCA markdown segment by name (filename without .md)."""
    try:
        from chat.backend.agent.skills.loader import load_core_prompt

        return load_core_prompt(BACKGROUND_RCA_SEGMENTS_DIR, segments=[segment_name]).strip()
    except Exception as e:
        logger.warning(f"Failed to load background RCA segment '{segment_name}': {e}")
        return ""


def _render_background_segment(
    segment_name: str,
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """Render a background segment with optional {variable} replacements."""
    template = _load_background_segment_template(segment_name)
    if not template:
        return ""

    if not context:
        return template

    try:
        from chat.backend.agent.skills.loader import resolve_template

        return resolve_template(template, context)
    except Exception as e:
        logger.warning(f"Failed to render background RCA segment '{segment_name}': {e}")
        return template


def _append_background_segment(
    parts: List[str],
    segment_name: str,
    context: Optional[Dict[str, Any]] = None,
    leading_blank: bool = False,
    trailing_blank: bool = False,
) -> None:
    """Append rendered background segment text with optional surrounding blanks."""
    rendered = _render_background_segment(segment_name, context=context)
    if not rendered:
        return

    if leading_blank:
        parts.append("")
    parts.append(rendered)
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
    _append_background_segment(
        parts,
        "background_header",
        context={
            "source_display": source_display,
            "providers_display": providers_display,
        },
        trailing_blank=True,
    )
    _append_background_segment(
        parts,
        "background_provider_tools",
        context={"providers_tools_display": providers_tools_display},
        leading_blank=True,
    )

    # Load integration-specific RCA guidance from skill files
    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        user_id = rca_context.get('user_id', '')
        registry = SkillRegistry.get_instance()
        rca_skills_content = registry.load_skills_for_rca(
            user_id=user_id,
            source=source,
            providers=providers,
            integrations=integrations,
        )
        if rca_skills_content:
            parts.extend(["", rca_skills_content])
    except Exception as e:
        logger.warning(f"Failed to load RCA skills: {e}")

    # Integration-specific guidance (Splunk, Datadog, GitHub, Jira, etc.)
    # now loaded from skill files above via SkillRegistry.load_skills_for_rca().

    _append_background_segment(parts, "background_knowledge_base", leading_blank=True)
    _append_background_segment(parts, "background_vm_access", leading_blank=True)
    _append_background_segment(
        parts,
        "background_context_update",
        leading_blank=True,
        trailing_blank=True,
    )

    # Critical requirements - MUST complete all before stopping
    if source == 'slack':
        _append_background_segment(parts, "background_source_slack", leading_blank=True)
    elif source == 'google_chat':
        _append_background_segment(parts, "background_source_google_chat", leading_blank=True)
    else:
        _append_background_segment(
            parts,
            "background_source_general",
            context={"providers_display": providers_display},
            leading_blank=True,
        )

        # Non-Anthropic models often don't produce text between tool calls unless instructed to
        model_name = (getattr(state, 'model', '') or '').lower()
        if model_name and not model_name.startswith("anthropic/"):
            _append_background_segment(parts, "background_source_general_non_anthropic")

        _append_background_segment(parts, "background_source_general_footer")

    return "\n".join(parts)
