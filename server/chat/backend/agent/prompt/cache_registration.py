from __future__ import annotations

from typing import Any, List

from .schema import PromptSegments

# Prefix Cache Configuration
PREFIX_CACHE_EPHEMERAL_TTL = 300  # 5 minutes - TTL for ephemeral cache segments


def register_prompt_cache_breakpoints(
    pcm: Any,
    segments: PromptSegments,
    tools: List[Any],
    provider: str,
    tenant_id: str,
) -> None:
    # Cache stable segments with regular TTL
    pcm.register_segment(
        segment_name="system_invariant",
        content=segments.system_invariant,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="provider_constraints",
        content=segments.provider_constraints,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    pcm.register_segment(
        segment_name="regional_rules",
        content=segments.regional_rules,
        provider=provider,
        tenant_id=tenant_id,
        ttl_s=None,
    )
    if segments.provider_context:
        pcm.register_segment(
            segment_name="provider_context",
            content=segments.provider_context,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.integration_index:
        pcm.register_segment(
            segment_name="integration_index",
            content=segments.integration_index,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.prerequisite_checks:
        pcm.register_segment(
            segment_name="prerequisite_checks",
            content=segments.prerequisite_checks,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.terraform_validation:
        pcm.register_segment(
            segment_name="terraform_validation",
            content=segments.terraform_validation,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.model_overlay:
        pcm.register_segment(
            segment_name="model_overlay",
            content=segments.model_overlay,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    if segments.failure_recovery:
        pcm.register_segment(
            segment_name="failure_recovery",
            content=segments.failure_recovery,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
    # Tie tool schema/version into a dedicated segment so cache invalidates when tool defs change
    pcm.register_segment(
        segment_name="tools_manifest",
        content="Tool definitions and parameter shapes",
        provider=provider,
        tenant_id=tenant_id,
        tools=tools,
        ttl_s=None,
    )
    # Ephemeral rules are not cached (or can be set to very short TTL if desired)
    if segments.ephemeral_rules:
        pcm.register_segment(
            segment_name="ephemeral_rules",
            content=segments.ephemeral_rules,
            provider=provider,
            tenant_id=tenant_id,
            ttl_s=PREFIX_CACHE_EPHEMERAL_TTL,
        )
