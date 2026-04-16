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


def build_system_invariant(is_background: bool = False) -> str:
    """Load core system prompt from modular markdown files under skills/core/.

    Segments are loaded in a fixed order that mirrors the original monolithic
    prompt so that cached prefixes remain stable across deployments.

    In background RCA mode, Terraform/IaC, SSH setup, and cloud CLI discovery
    segments are omitted (~3,300 tokens) since background investigations are
    read-only and the freed budget is better spent on integration skills.
    """
    from chat.backend.agent.skills.loader import load_core_prompt

    core_dir = os.path.join(
        os.path.dirname(__file__), os.pardir, "skills", "core"
    )
    core_dir = os.path.normpath(core_dir)

    if is_background:
        return load_core_prompt(core_dir, segments=[
            "identity",
            "knowledge_base",
            "error_handling",
            "investigation",
            "behavioral_rules",
        ])

    return load_core_prompt(core_dir, segments=[
        "identity",
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

    # Build system invariant — trimmed in background mode to free tokens for skills
    is_background = bool(state and getattr(state, 'is_background', False))
    system_invariant = build_system_invariant(is_background=is_background)

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

    # Build background mode segment if applicable (for RCA background chats)
    background_mode = build_background_mode_segment(state)

    # Build skills-based integration index (agent loads details via load_skill tool)
    integration_index = ""
    if state and hasattr(state, 'user_id'):
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
    )


def assemble_system_prompt(segments: PromptSegments) -> str:  # main prompt builder
    parts: List[str] = []
    # Background mode comes first if present (important RCA context)
    if segments.background_mode:
        parts.append(segments.background_mode)
    # Knowledge base memory comes early (user-provided context for all investigations)
    if segments.knowledge_base_memory:
        parts.append(segments.knowledge_base_memory)
    if segments.ephemeral_rules:
        parts.append(segments.ephemeral_rules)
    if segments.model_overlay:
        parts.append(segments.model_overlay)
    if segments.provider_context:
        parts.append(segments.provider_context)
    if segments.manual_vm_access:
        parts.append(segments.manual_vm_access)
    # Skills-based: compact index of connected integrations
    if segments.integration_index:
        parts.append(segments.integration_index)
    if segments.prerequisite_checks:
        parts.append(segments.prerequisite_checks)
    parts.append(segments.system_invariant)
    parts.append(segments.provider_constraints)
    parts.append(segments.regional_rules)
    if segments.long_documents_note:
        parts.append(segments.long_documents_note)
    if segments.terraform_validation and not segments.background_mode:
        parts.append(segments.terraform_validation)
    if segments.failure_recovery and not segments.background_mode:
        parts.append(segments.failure_recovery)
    return "\n".join(parts)
