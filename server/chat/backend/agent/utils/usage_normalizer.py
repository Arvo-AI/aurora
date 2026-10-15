"""Normalize provider token usage into one shape for cost and DB tracking.

LangChain's convention: ``input_tokens`` is the TOTAL input and
``input_token_details.cache_read`` / ``cache_creation`` are subsets of it.
langchain-aws's Converse client breaks that: it reports Bedrock's ``inputTokens``,
which EXCLUDES cached tokens, so subtracting the cache count from it undercounts cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


_CACHE_WRITE_KEYS = ("cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


@dataclass(frozen=True)
class NormalizedUsage:
    """Token counts where ``input_tokens`` always includes cache reads and writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _as_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: Optional[Mapping[str, Any]]) -> NormalizedUsage:
    """Accept LangChain ``usage_metadata`` or an OpenAI-style ``token_usage`` dict."""
    if not isinstance(usage, Mapping):
        return NormalizedUsage()

    input_tokens = _as_int(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = _as_int(usage.get("output_tokens", usage.get("completion_tokens")))

    cache_read = cache_write = 0
    input_details = usage.get("input_token_details")
    if isinstance(input_details, Mapping):
        cache_read = _as_int(input_details.get("cache_read"))
        # langchain-anthropic zeroes "cache_creation" when it reports the per-TTL
        # keys instead, so summing all three never double counts
        cache_write = sum(_as_int(input_details.get(k)) for k in _CACHE_WRITE_KEYS)
    else:
        # OpenAI-style raw usage: cached tokens live under prompt_tokens_details
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping):
            cache_read = _as_int(prompt_details.get("cached_tokens"))

    cache_total = cache_read + cache_write
    total_tokens = _as_int(usage.get("total_tokens"))

    # Converse's total is input + output + cache read + cache write, so a total that
    # matches that sum means ``input_tokens`` left the cache tokens out. Inclusive
    # providers (Anthropic, OpenAI) report total == input + output and skip this.
    if cache_total and total_tokens == input_tokens + output_tokens + cache_total:
        input_tokens += cache_total

    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
