"""Anthropic prompt-cache control for the agent system prompt.

Anthropic bills the cached prefix (tools + system + messages up to a
``cache_control`` breakpoint) at 0.1x base input price on a cache READ, versus
1.0x for a full re-send. The agent re-sends a large, stable system prompt on
every ReAct turn, so marking that prompt with an ephemeral breakpoint turns
turns 2..N of a session into cache reads.

Mechanism (verified against Anthropic / OpenRouter / LangChain docs, 2026):
``cache_control`` is a PER-CONTENT-BLOCK marker, not a top-level request field.
We hand ``create_agent`` a ``SystemMessage`` whose ``content`` is a single text
block carrying ``cache_control``. Both prod paths transmit it to the wire:
  - OpenRouter (langchain ``ChatOpenAI``): ``_format_message_content`` passes
    unknown content-block keys through verbatim, so ``cache_control`` reaches
    OpenRouter, which forwards it to Anthropic.
  - Direct (langchain ``ChatAnthropic``): natively reads ``cache_control`` on
    content blocks.
Using a per-block breakpoint (not OpenRouter's top-level field) avoids pinning
routing to Anthropic-direct, so Bedrock/Vertex endpoints stay eligible.

ponytail: single breakpoint on the whole system prompt; char-length min-prefix
heuristic. Upgrade path if hit-rate drops: add a second breakpoint on the tool
manifest / last large turn, or switch to a real token count.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)

# Smallest cacheable prefix is model-specific (Sonnet/Opus 4.8 = 1024 tokens,
# Opus 4.5/4.6 & Haiku 4.5 = 4096). We gate on characters, not tokens, to stay
# import-light. ~4 chars/token, and we use the largest minimum (4096 tokens) so
# we never waste a cache WRITE on a prompt too small to cache on any model.
# ponytail: char heuristic; upgrade path = count tokens via the model tokenizer.
_MIN_PREFIX_CHARS = 4096 * 4

_VALID_TTLS = {"5m", "1h"}


def _caching_enabled() -> bool:
    # Default on: a cache miss only costs a normal call plus a small write
    # premium, and it degrades gracefully. Disable with PROMPT_CACHING_ENABLED=false.
    return os.getenv("PROMPT_CACHING_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


def _cache_ttl() -> str:
    ttl = os.getenv("PROMPT_CACHE_TTL", "5m").strip().lower()
    return ttl if ttl in _VALID_TTLS else "5m"


def _is_anthropic_model(model_name: str) -> bool:
    if not model_name:
        return False
    # OpenAI/Gemini auto-cache server-side and ignore (or reject) cache_control,
    # so we only tag Anthropic-family models. Match the OpenRouter id prefix.
    return model_name.split("/", 1)[0].lower() == "anthropic"


def build_cached_system_prompt(model_name: str, system_prompt_text: str) -> Any:
    """Return a system prompt for ``create_agent`` with Anthropic caching applied.

    Returns a ``SystemMessage`` carrying a ``cache_control`` ephemeral breakpoint
    when caching is enabled, the model is Anthropic-family, and the prompt is
    large enough to be cacheable. Otherwise returns the original string unchanged
    (no-op), so non-Anthropic models and tiny prompts are unaffected.
    """
    if not system_prompt_text or not isinstance(system_prompt_text, str):
        return system_prompt_text
    if not _caching_enabled():
        return system_prompt_text
    if not _is_anthropic_model(model_name):
        return system_prompt_text
    if len(system_prompt_text) < _MIN_PREFIX_CHARS:
        # Too small to cache on any Anthropic model; a breakpoint here would just
        # cost a write that can never be read back.
        return system_prompt_text

    cache_control = {"type": "ephemeral"}
    ttl = _cache_ttl()
    if ttl != "5m":
        cache_control["ttl"] = ttl

    logger.info(
        "Applying Anthropic prompt cache breakpoint (ttl=%s, prefix_chars=%d)",
        ttl,
        len(system_prompt_text),
    )
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": system_prompt_text,
                "cache_control": cache_control,
            }
        ]
    )
