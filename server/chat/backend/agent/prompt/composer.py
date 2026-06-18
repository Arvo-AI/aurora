from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from .background import build_background_mode_segment
from .context_fetchers import (
    build_knowledge_base_memory_segment,
    build_manual_vm_access_segment,
)
from .provider_rules import (
    build_ephemeral_rules,
    build_failure_recovery_segment,
    build_long_documents_note,
    build_model_overlay_segment,
    build_prerequisite_segment,
    build_provider_constraints,
    build_provider_context_segment,
    build_regional_rules,
    build_terraform_validation_segment,
)
from .schema import PromptSegments

_RCA_SECTIONS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "rca_sections")
)

_RCA_SECTION_ORDER = [
    "identity",
    "investigation",
    "context_mgmt",
    "error_recovery",
    "evidence_standard",
    "conclusion_gate",
]


def _build_rca_system_prompt() -> str:
    """Assemble the background RCA system prompt from rca_sections/*.md."""
    parts = []
    for name in _RCA_SECTION_ORDER:
        path = os.path.join(_RCA_SECTIONS_DIR, f"{name}.md")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            parts.append(content)

    return "\n\n".join(parts)


def build_system_invariant(is_background: bool = False) -> str:
    """Build the system prompt for interactive or background RCA mode."""
    from chat.backend.agent.skills.loader import load_core_prompt

    if is_background:
        return _build_rca_system_prompt()

    core_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), os.pardir, "skills", "core")
    )
    return load_core_prompt(core_dir, segments=[
        "identity",
        "security",
        "knowledge_base",
        "tool_selection",
        "ssh_access",
        "cloud_access",
        "error_handling",
        "investigation",
        "behavioral_rules",
    ])


def build_prompt_segments(
    provider_preference: Optional[Any],
    mode: Optional[str],
    has_zip_reference: bool,
    state: Optional[Any] = None,
) -> PromptSegments:
    _, _, provider_constraints = build_provider_constraints(provider_preference)

    # Detect background type: actions get full invariant, RCA gets trimmed
    is_background = bool(state and getattr(state, 'is_background', False))
    rca_context = getattr(state, 'rca_context', None) or {}
    is_action = rca_context.get('source') == 'action'

    # Actions need tool_selection, cloud_access, ssh_access — use full invariant
    system_invariant = build_system_invariant(is_background=is_background and not is_action)

    provider_context = build_provider_context_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
        mode=mode,
    )

    prerequisite_checks = build_prerequisite_segment(
        provider_preference=provider_preference,
        selected_project_id=getattr(state, 'selected_project_id', None) if state else None,
    )

    terraform_validation = build_terraform_validation_segment(state)

    model_overlay = build_model_overlay_segment(
        getattr(state, 'model', None) if state else None,
        provider_preference=provider_preference,
    )

    failure_recovery = build_failure_recovery_segment(state)
    manual_vm_access = build_manual_vm_access_segment(getattr(state, "user_id", None))

    # Build background mode segment: action-specific or RCA
    if is_action:
        from .background import build_action_mode_segment
        background_mode = build_action_mode_segment(state)
    else:
        background_mode = build_background_mode_segment(state)

    # Build skills index for interactive chat — agent calls load_skill on demand
    integration_index = ""
    if state and hasattr(state, 'user_id') and not is_background:
        try:
            from chat.backend.agent.skills.registry import SkillRegistry
            registry = SkillRegistry.get_instance()
            integration_index = registry.build_index(state.user_id)
        except Exception as e:
            logging.warning(f"Failed to build skills index: {e}")

    # Build knowledge base memory context for authenticated users
    knowledge_base_memory = ""
    if state and hasattr(state, 'user_id'):
        knowledge_base_memory = build_knowledge_base_memory_segment(state.user_id)

    # Build org-level command policy segment
    security_policy = ""
    if state and hasattr(state, 'user_id'):
        try:
            from utils.auth.stateless_auth import get_org_id_for_user
            from utils.auth.command_policy import get_policy_prompt_text
            org_id = get_org_id_for_user(state.user_id)
            if org_id:
                security_policy = get_policy_prompt_text(org_id)
        except Exception as e:
            logging.error("Failed to build security policy segment: %s", e)
            security_policy = (
                "IMPORTANT: This organization has command policies but they could not be loaded. "
                "Warn the user before running commands, as they may be denied by policy enforcement."
            )

    return PromptSegments(
        system_invariant=system_invariant,
        provider_constraints=provider_constraints,
        regional_rules=build_regional_rules(),
        ephemeral_rules=build_ephemeral_rules(mode),
        long_documents_note=build_long_documents_note(has_zip_reference),
        provider_context=provider_context,
        prerequisite_checks=prerequisite_checks,
        terraform_validation=terraform_validation,
        model_overlay=model_overlay,
        failure_recovery=failure_recovery,
        background_mode=background_mode,
        manual_vm_access=manual_vm_access,
        knowledge_base_memory=knowledge_base_memory,
        integration_index=integration_index,
        security_policy=security_policy,
        is_rca_background=is_background and not is_action,
    )


def assemble_system_prompt(segments: PromptSegments) -> str:  # main prompt builder
    parts: List[str] = []

    # Ordered optional segments
    for segment in (
        segments.security_policy,
        segments.background_mode,
        segments.knowledge_base_memory,
        segments.ephemeral_rules,
        segments.model_overlay,
        segments.provider_context,
        segments.manual_vm_access,
        segments.integration_index,
        segments.prerequisite_checks,
    ):
        if segment:
            parts.append(segment)

    parts.append(segments.system_invariant)
    parts.append(segments.provider_constraints)
    parts.append(segments.regional_rules)
    if segments.long_documents_note:
        parts.append(segments.long_documents_note)

    is_rca_background = segments.is_rca_background
    if segments.terraform_validation and not is_rca_background:
        parts.append(segments.terraform_validation)
    if segments.failure_recovery and not is_rca_background:
        parts.append(segments.failure_recovery)
    if segments.security_policy:
        parts.append("REMINDER: Commands that violate the organization policy will be rejected. Do not attempt workarounds.")
    return "\n".join(parts)
