"""Prompt-cache markers for the agent system prompt, chosen by the serving provider.

A cache READ bills at ~0.1x input price, and the agent re-sends the same large system
prompt on every ReAct turn. Each provider declares how it accepts a marker via
``BaseLLMProvider.prompt_cache_style``; no declared style means no markers.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional

from langchain_core.messages import BaseMessage, SystemMessage

if TYPE_CHECKING:
    from chat.backend.agent.providers.base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)

# Marker styles a provider can declare. Defined here, not in providers/, so providers
# can import them without a cycle (this module never imports the providers package).
CACHE_STYLE_CACHE_CONTROL = "cache_control"  # Anthropic-style flag on a content block
CACHE_STYLE_BEDROCK = "bedrock"  # Converse standalone cachePoint block

BEDROCK_CACHE_POINT = {"cachePoint": {"type": "default"}}
_EPHEMERAL = {"type": "ephemeral"}

# Smallest cacheable prefix per Claude family, in tokens (Bedrock prompt-caching
# table; first prefix match wins, so longer names precede their shorter prefixes).
# A breakpoint on a shorter prefix is accepted but caches nothing.
_MIN_CACHEABLE_TOKENS = (
    ("claude-haiku-5-5", 512),
    ("claude-opus-5-5", 512),
    ("claude-sonnet-5-5", 512),
    ("claude-fable-5", 512),
    ("claude-opus-5", 512),
    ("claude-sonnet-5", 1024),
    ("claude-opus-4-8", 1024),
    ("claude-sonnet-4-6", 1024),
    ("claude-sonnet-4-5", 1024),
    ("claude-opus-4-5", 4096),
    ("claude-opus-4-6", 4096),
    ("claude-opus-4-7", 4096),
)
# Unknown families get the largest published minimum, so a write is never wasted
_DEFAULT_MIN_CACHEABLE_TOKENS = 4096
# ponytail: ~4 chars/token heuristic, not a tokenizer
_CHARS_PER_TOKEN = 4

_GEO_PREFIXES = ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "us-gov.")
_VENDOR_PREFIXES = ("anthropic/", "anthropic.")
# Bedrock suffixes like ``-20250929-v1:0`` / ``-v1:0`` / ``-20250929``
_VERSION_SUFFIX = re.compile(r"(-\d{8})?(-v\d+(:\d+)?)?$")


def claude_family(model_id: str) -> str:
    """Dashed family name for any spelling of a Claude id.

    ``bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0`` and
    ``anthropic/claude-sonnet-4.5`` both give ``claude-sonnet-4-5``.
    """
    name = (model_id or "").strip().lower()
    if name.startswith("bedrock/"):
        name = name[len("bedrock/"):]
    for prefix in _GEO_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for prefix in _VENDOR_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return _VERSION_SUFFIX.sub("", name).replace(".", "-")


def min_cacheable_chars(model_id: str) -> int:
    """Shortest prefix, in chars, that ``model_id`` will actually cache."""
    family = claude_family(model_id)
    for prefix, tokens in _MIN_CACHEABLE_TOKENS:
        if family.startswith(prefix):
            return tokens * _CHARS_PER_TOKEN
    return _DEFAULT_MIN_CACHEABLE_TOKENS * _CHARS_PER_TOKEN


def resolve_cache_style(provider: Optional["BaseLLMProvider"], model_name: str) -> Optional[str]:
    """The provider's declared marker style for ``model_name``, or None on any doubt."""
    # Unknown serving provider: never guess a marker dialect
    if provider is None:
        return None
    try:
        return provider.prompt_cache_style(model_name)
    except Exception:
        logger.warning("Could not resolve prompt cache style; skipping", exc_info=True)
        return None


def _system_blocks(style: str, static_text: str, dynamic_text: str) -> Optional[List[dict]]:
    """System content with a breakpoint after ``static_text``, in the provider's dialect."""
    # Anthropic API / OpenRouter / gateways: flag rides on the text block itself. The
    # dynamic block gets one too, so turns 2..N also read the per-session suffix.
    if style == CACHE_STYLE_CACHE_CONTROL:
        blocks = [{"type": "text", "text": static_text, "cache_control": dict(_EPHEMERAL)}]
        if dynamic_text:
            blocks.append({"type": "text", "text": dynamic_text, "cache_control": dict(_EPHEMERAL)})
        return blocks

    # Bedrock Converse drops cache_control; it only honors a standalone cachePoint
    # block. The suffix is covered by the last-message point (BedrockPromptCacheMiddleware).
    if style == CACHE_STYLE_BEDROCK:
        blocks = [{"type": "text", "text": static_text}, dict(BEDROCK_CACHE_POINT)]
        if dynamic_text:
            blocks.append({"type": "text", "text": dynamic_text})
        return blocks

    return None


def build_cached_system_prompt(
    model_name: str,
    static_text: str,
    dynamic_text: str = "",
    *,
    provider: Optional["BaseLLMProvider"] = None,
) -> "str | SystemMessage":
    """System prompt with a cache breakpoint after the cross-session static prefix.

    Falls back to one breakpoint at the end of the whole prompt when only the whole
    prompt reaches the model's minimum, and to the plain string when nothing does,
    the provider declares no style, or anything unexpected goes wrong.
    """
    static_text = static_text if isinstance(static_text, str) else ""
    dynamic_text = dynamic_text if isinstance(dynamic_text, str) else ""
    plain = "\n".join(part for part in (static_text, dynamic_text) if part)
    if not plain:
        return plain

    style = resolve_cache_style(provider, model_name)
    # Provider caches implicitly or not at all: no markers to send
    if not style:
        return plain

    min_chars = min_cacheable_chars(model_name)
    try:
        # Shared prefix is cacheable on its own: breakpoint between static and dynamic
        if len(static_text) >= min_chars:
            blocks = _system_blocks(style, static_text, dynamic_text)
        # Only the whole prompt is big enough: keep the per-session read on turns 2..N
        elif len(plain) >= min_chars:
            blocks = _system_blocks(style, plain, "")
        # Too small to cache on this model; a write here could never be read back
        else:
            return plain

        # Style we don't know how to render: stay uncached
        if blocks is None:
            return plain
        logger.info(
            "Applying prompt cache marker (style=%s, static_chars=%d, dynamic_chars=%d)",
            style,
            len(static_text),
            len(dynamic_text),
        )
        return SystemMessage(content=blocks)
    except Exception:
        # Caching must never break the agent: degrade to the uncached prompt
        logger.warning("Failed to build cached system prompt; using plain prompt", exc_info=True)
        return plain


def _is_bedrock_cache_point(block: Any) -> bool:
    return isinstance(block, dict) and "cachePoint" in block


def with_bedrock_tools_cache_point(tools: List[Any]) -> List[Any]:
    """``tools`` plus a trailing Bedrock cachePoint, so tool definitions are cached."""
    # No tools, or another layer already marked them
    if not tools or any(_is_bedrock_cache_point(t) for t in tools):
        return tools
    return [*tools, dict(BEDROCK_CACHE_POINT)]


def with_bedrock_last_message_cache_point(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Copy of ``messages`` whose last message ends with a Bedrock cachePoint.

    The original message is left untouched so the marker never lands in graph state.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.content
    if isinstance(content, str):
        # Empty text would become a placeholder block; nothing worth marking
        if not content.strip():
            return messages
        content = [{"type": "text", "text": content}]
    elif not isinstance(content, list) or not content:
        return messages
    # Already marked (e.g. a retry of the same request)
    if any(_is_bedrock_cache_point(b) for b in content):
        return messages
    marked = last.model_copy(update={"content": [*content, dict(BEDROCK_CACHE_POINT)]})
    return [*messages[:-1], marked]


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
            if _is_bedrock_cache_point(block):
                continue
            # cache_control rides on a real block; keep the block, drop the flag
            if isinstance(block, dict) and "cache_control" in block:
                block = {k: v for k, v in block.items() if k != "cache_control"}
            blocks.append(block)
        cleaned.append(msg.model_copy(update={"content": blocks}))
    return cleaned


def strip_bedrock_tool_cache_points(kwargs: dict) -> dict:
    """Copy of a model call's kwargs with Bedrock cachePoint entries removed from bound tools."""
    tools = kwargs.get("tools")
    tool_config = kwargs.get("toolConfig")
    # Nothing bound, nothing to strip
    if not tools and not tool_config:
        return kwargs
    cleaned = dict(kwargs)
    if tools:
        cleaned["tools"] = [t for t in tools if not _is_bedrock_cache_point(t)]
    # Nova system tools bind a prebuilt toolConfig instead of a tools list
    if isinstance(tool_config, dict) and tool_config.get("tools"):
        cleaned["toolConfig"] = {
            **tool_config,
            "tools": [t for t in tool_config["tools"] if not _is_bedrock_cache_point(t)],
        }
    return cleaned
