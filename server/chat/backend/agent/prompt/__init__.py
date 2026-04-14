 
"""Prompt assembly package.

Module layout:
- prompt_builder.py: backward-compatible facade
- composer.py: top-level segment composition
- provider_rules.py: provider/mode/rule text segments
- context_fetchers.py: DB-backed prompt context helpers
- background.py: background RCA prompt assembly
- cache_registration.py: prefix-cache segment registration
- schema.py: PromptSegments dataclass
"""

from .prompt_builder import (
    CLOUD_EXEC_PROVIDERS,
    PREFIX_CACHE_EPHEMERAL_TTL,
    PromptSegments,
    assemble_system_prompt,
    build_background_mode_segment,
    build_ephemeral_rules,
    build_failure_recovery_segment,
    build_knowledge_base_memory_segment,
    build_long_documents_note,
    build_manual_vm_access_segment,
    build_model_overlay_segment,
    build_prerequisite_segment,
    build_prompt_segments,
    build_provider_constraints,
    build_provider_context_segment,
    build_regional_rules,
    build_system_invariant,
    build_terraform_validation_segment,
    build_web_search_note,
    register_prompt_cache_breakpoints,
)

__all__ = [
    "CLOUD_EXEC_PROVIDERS",
    "PREFIX_CACHE_EPHEMERAL_TTL",
    "PromptSegments",
    "assemble_system_prompt",
    "build_background_mode_segment",
    "build_ephemeral_rules",
    "build_failure_recovery_segment",
    "build_knowledge_base_memory_segment",
    "build_long_documents_note",
    "build_manual_vm_access_segment",
    "build_model_overlay_segment",
    "build_prerequisite_segment",
    "build_prompt_segments",
    "build_provider_constraints",
    "build_provider_context_segment",
    "build_regional_rules",
    "build_system_invariant",
    "build_terraform_validation_segment",
    "build_web_search_note",
    "register_prompt_cache_breakpoints",
]
