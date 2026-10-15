"""Prompt-cache markers for the agent system prompt, chosen by the serving provider.

A cache READ bills at ~0.1x input price, and the agent re-sends the same large system
prompt on every ReAct turn. Each provider declares how it accepts a marker via
``BaseLLMProvider.prompt_cache_style``; no declared style means no markers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

from langchain_core.messages import BaseMessage, SystemMessage

if TYPE_CHECKING:
    from chat.backend.agent.providers.base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)

# Marker styles a provider can declare. Defined here, not in providers/, so providers
# can import them without a cycle (this module never imports the providers package).
CACHE_STYLE_CACHE_CONTROL = "cache_control"  # Anthropic-style flag on a content block
CACHE_STYLE_CACHE_POINT = "cache_point"  # Bedrock Converse standalone cachePoint block

# Smallest cacheable prefix is model-specific (1024 tokens on Sonnet, 4096 on Opus 4.5 /
# Haiku 4.5). Gate on chars at the largest minimum (~4 chars/token) so a cache write is
# never wasted on a prompt too small to cache. ponytail: char heuristic, not a tokenizer.
_MIN_PREFIX_CHARS = 4096 * 4


def _marker_blocks(style: str, text: str) -> Optional[List[dict]]:
    """Content blocks carrying ``text`` plus a cache marker in the provider's dialect."""
    # Anthropic API / OpenRouter / gateways: flag rides on the text block itself
    if style == CACHE_STYLE_CACHE_CONTROL:
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # Bedrock Converse drops cache_control; it only honors a standalone cachePoint block
    if style == CACHE_STYLE_CACHE_POINT:
        return [{"type": "text", "text": text}, {"cachePoint": {"type": "default"}}]

    return None


def build_cached_system_prompt(
    model_name: str,
    system_prompt_text: str,
    provider: Optional["BaseLLMProvider"] = None,
) -> "str | SystemMessage":
    """Return ``system_prompt_text`` as a cache-marked ``SystemMessage`` when possible.

    Falls back to the plain string (original behavior) when the provider declares no
    style, the prompt is too small to cache, or anything unexpected goes wrong.
    """
    if not system_prompt_text or not isinstance(system_prompt_text, str):
        return system_prompt_text

    # Unknown serving provider: never guess a marker dialect
    if provider is None:
        return system_prompt_text

    try:
        style = provider.prompt_cache_style(model_name)
    except Exception:
        logger.warning("Could not resolve prompt cache style; skipping", exc_info=True)
        return system_prompt_text

    # Provider caches implicitly or not at all: no markers to send
    if not style:
        return system_prompt_text

    # Too small to cache on any model; a write here could never be read back
    if len(system_prompt_text) < _MIN_PREFIX_CHARS:
        return system_prompt_text

    try:
        blocks = _marker_blocks(style, system_prompt_text)
        # Style we don't know how to render: stay uncached
        if blocks is None:
            return system_prompt_text
        logger.info(
            "Applying prompt cache marker (style=%s, prefix_chars=%d)",
            style,
            len(system_prompt_text),
        )
        return SystemMessage(content=blocks)
    except Exception:
        # Caching must never break the agent: degrade to the uncached prompt
        logger.warning("Failed to build cached system prompt; using plain prompt", exc_info=True)
        return system_prompt_text


def strip_cache_markers(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Copy of ``messages`` with every cache marker removed (for retry after a rejection)."""
    cleaned: List[BaseMessage] = []
    for msg in messages:
        content = msg.content
        # String content never carries markers
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        blocks: List[Any] = []
        for block in content:
            # A standalone cachePoint block exists only to mark; drop it entirely
            if isinstance(block, dict) and "cachePoint" in block:
                continue
            # cache_control rides on a real block; keep the block, drop the flag
            if isinstance(block, dict) and "cache_control" in block:
                block = {k: v for k, v in block.items() if k != "cache_control"}
            blocks.append(block)
        cleaned.append(msg.model_copy(update={"content": blocks}))
    return cleaned
