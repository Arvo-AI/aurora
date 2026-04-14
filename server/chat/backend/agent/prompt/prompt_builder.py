"""
Backward-compatible facade for prompt assembly modules.

The original monolithic prompt builder was split into focused modules:
- provider_rules.py
- context_fetchers.py
- background.py
- composer.py
- cache_registration.py
"""

from .background import build_background_mode_segment
from .cache_registration import (
    PREFIX_CACHE_EPHEMERAL_TTL,
    register_prompt_cache_breakpoints,
)
from .composer import (
    assemble_system_prompt,
    build_prompt_segments,
    build_system_invariant,
)
from .context_fetchers import (
    build_knowledge_base_memory_segment,
    build_manual_vm_access_segment,
)
from .provider_rules import (
    CLOUD_EXEC_PROVIDERS,
    build_ephemeral_rules,
    build_failure_recovery_segment,
    build_long_documents_note,
    build_model_overlay_segment,
    build_prerequisite_segment,
    build_provider_constraints,
    build_provider_context_segment,
    build_regional_rules,
    build_terraform_validation_segment,
    build_web_search_note,
)
from .schema import PromptSegments

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
