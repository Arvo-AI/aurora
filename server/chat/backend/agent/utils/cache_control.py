"""Prompt-cache breakpoints for the agent system prompt (Bedrock + Anthropic).

Claude bills the cached prefix (tools + system + messages up to a breakpoint) at
~0.1x base input price on a cache READ versus 1.0x for a full re-send. The agent
re-sends a large, stable system prompt on every ReAct turn, so a breakpoint
after the static part of that prompt turns turns 2..N of a session — and the
first turn of every other session that shares the prefix — into cache reads.

``agent.py`` splits the prompt with ``assemble_system_prompt_parts`` into a
STATIC prefix (core invariant, provider constraints, regional rules; identical
across sessions/orgs/users with the same providers) and a DYNAMIC suffix
(security policy, incident/skills data, memories with ages, ...). The breakpoint
goes between the two, so the dynamic text never invalidates the shared prefix.

Three wire formats, chosen by the provider that actually SERVES the call (not by
the id prefix — ``bedrock/eu.anthropic.claude-...`` and ``anthropic/claude-...``
may both be served by Bedrock):

- **Bedrock native** (``langchain_aws.ChatBedrockConverse``): Converse API
  ``{"cachePoint": {"type": "default"}}`` blocks. The library's
  ``_lc_content_to_bedrock`` passes through blocks that carry no ``type`` key
  verbatim and DROPS Anthropic-style ``cache_control`` (verified in langchain-aws
  1.9.0), so the system list is built as raw Converse blocks:
  ``[{"text": static}, {"cachePoint": ...}, {"text": dynamic}]``. The tools and
  last-message checkpoints are added per request by ``BedrockPromptCacheMiddleware``
  (``model_settings["cache_control"]`` → ``ChatBedrockConverse._apply_cache_points``),
  which skips its own system-end point when the system already carries one, so a
  request holds 3 cache points (tools, static system, last message) — under the
  Bedrock maximum of 4.
- **Anthropic API / OpenRouter** (``ChatAnthropic``; OpenRouter via ``ChatOpenAI``,
  which passes unknown content-block keys through): per-content-block
  ``cache_control: {"type": "ephemeral"}`` markers. When the static prefix alone
  reaches the model's cacheable minimum, two breakpoints (static block, dynamic
  block; Anthropic allows four). When only the whole prompt reaches it, the single
  end-of-system breakpoint on one joined block — the exact shape the pre-split
  code sent — so a model with a 4,096-token minimum (Haiku 4.5, Opus 4.5-4.7)
  keeps its cache read on every turn of a background RCA.
- **Bedrock gateway** (``BEDROCK_BASE_URL`` set, ``ChatOpenAI``): unchanged from
  before the split. Only ``anthropic/``-prefixed ids get the Anthropic-style
  markers (they already did); an explicit ``bedrock/<id>`` — the documented
  gateway configuration — keeps the plain string, because an OpenAI-shaped
  gateway caches (or not) through its own switches and may reject list-form
  system content.
- **Everything else** (Google, OpenAI, Ollama, non-Claude ids, a prompt below the
  model's cacheable minimum): the plain joined string, exactly as before caching.

TTL is the default 5 minutes (no explicit ``ttl``): it refreshes on every read,
so it covers virtually every inter-turn gap, and a 1h entry costs 2x to write.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from langchain_core.messages import SystemMessage

from chat.backend.agent.call_options import claude_family, min_cacheable_tokens

logger = logging.getLogger(__name__)

# Bedrock ignores (no error) a checkpoint whose prefix is shorter than the model's
# minimum — 512 tokens for the Claude 5.x generation, 1,024 for Sonnet 5 / 4.x,
# 4,096 for Opus 4.5-4.7 / Haiku 4.5 (Bedrock prompt-caching page, read 2026-10-22;
# table in ``call_options.min_cacheable_tokens``). We gate on characters to stay
# import-light: ~4 chars/token for English prose, so a prefix of
# ``min_tokens * 4`` chars is at least ``min_tokens`` tokens in practice. Below
# that a breakpoint would only cost a cache WRITE that can never be read back.
# ponytail: char heuristic; upgrade path = count tokens via the model tokenizer.
CHARS_PER_TOKEN = 4

# Converse API cache checkpoint (``type`` is always ``default``; TTL 5m when omitted).
BEDROCK_CACHE_POINT = {"cachePoint": {"type": "default"}}
# Anthropic Messages API breakpoint (5-minute ephemeral TTL when ``ttl`` is omitted).
ANTHROPIC_CACHE_CONTROL = {"type": "ephemeral"}

# Serving providers that speak the Anthropic Messages API directly or through an
# OpenAI-shaped proxy that forwards per-block ``cache_control`` to Anthropic.
# The Bedrock gateway is NOT here: it is handled per id prefix (see
# ``_has_anthropic_prefix``) so its wire shape stays what it was before caching
# was reworked.
_ANTHROPIC_STYLE_PROVIDERS = frozenset({"anthropic", "openrouter"})


def is_claude_model(model_name: str) -> bool:
    """True for any spelling of a Claude id (``anthropic/``, ``bedrock/<geo>.anthropic.``).

    OpenAI/Gemini auto-cache server-side and ignore (or reject) cache markers, so
    only the Claude family is tagged.
    """
    if not model_name:
        return False
    return claude_family(model_name).startswith("claude")


def _has_anthropic_prefix(model_name: str) -> bool:
    """True for ``anthropic/...`` ids — the only ids the Bedrock gateway path tags.

    Before the static/dynamic split the gateway received Anthropic-style
    ``cache_control`` blocks for exactly these ids (and a plain string for every
    other id, ``bedrock/<id>`` included); the gateway path keeps that contract.
    """
    return (model_name or "").split("/", 1)[0].strip().lower() == "anthropic"


def min_cacheable_chars(model_name: str) -> int:
    """Shortest static prefix (chars) worth a breakpoint on ``model_name``."""
    return min_cacheable_tokens(model_name) * CHARS_PER_TOKEN


def _plain(static_text: str, dynamic_text: str) -> str:
    return "\n".join(part for part in (static_text, dynamic_text) if part)


def _bedrock_system_blocks(static_text: str, dynamic_text: str) -> List[dict]:
    """Raw Converse system blocks: static text, cache point, then dynamic text."""
    blocks: List[dict] = [{"text": static_text}, dict(BEDROCK_CACHE_POINT)]
    if dynamic_text:
        blocks.append({"text": dynamic_text})
    return blocks


def _anthropic_system_blocks(static_text: str, dynamic_text: str) -> List[dict]:
    """Anthropic-style text blocks, each ending a cacheable prefix (two breakpoints)."""
    blocks: List[dict] = [
        {"type": "text", "text": static_text, "cache_control": dict(ANTHROPIC_CACHE_CONTROL)}
    ]
    if dynamic_text:
        blocks.append(
            {"type": "text", "text": dynamic_text, "cache_control": dict(ANTHROPIC_CACHE_CONTROL)}
        )
    return blocks


def _anthropic_single_breakpoint(system_text: str) -> List[dict]:
    """One joined text block with the breakpoint at the end of the system prompt.

    Used when the static prefix alone is under the model's minimum but the whole
    prompt is not: the shared-prefix hit is unavailable on that model anyway, and
    this keeps the whole-system cache read on turns 2..N of a session.
    """
    return [{"type": "text", "text": system_text, "cache_control": dict(ANTHROPIC_CACHE_CONTROL)}]


def _bedrock_native_blocks(
    model_name: str, static_text: str, dynamic_text: str, min_chars: int
) -> Optional[Tuple[List[dict], str]]:
    """Converse ``cachePoint`` system blocks, or ``None`` when the static prefix is
    too short to be worth a manual checkpoint."""
    if len(static_text) < min_chars:
        # No manual system point: ``BedrockPromptCacheMiddleware`` still has
        # langchain-aws place its own system-end / last-message / tools
        # points on the plain string, so the whole-system read is kept.
        logger.debug(
            "Prompt cache: no static system checkpoint for %s (static prefix "
            "%d chars < minimum %d); library points only",
            model_name, len(static_text), min_chars,
        )
        return None
    return _bedrock_system_blocks(static_text, dynamic_text), "bedrock cachePoint"


def _anthropic_style_blocks(
    model_name: str, static_text: str, dynamic_text: str, plain: str, min_chars: int
) -> Optional[Tuple[List[dict], str]]:
    """Anthropic Messages API ``cache_control`` blocks: static + system-end breakpoints
    when the static prefix is cacheable, the single system-end breakpoint when only
    the whole prompt is, ``None`` when even that is below the model's minimum."""
    if len(static_text) >= min_chars:
        return _anthropic_system_blocks(static_text, dynamic_text), "anthropic cache_control (static + system end)"
    if len(plain) >= min_chars:
        return _anthropic_single_breakpoint(plain), "anthropic cache_control (system end)"
    # Too small to cache on this model even as a whole; a breakpoint would just
    # cost a write that can never be read back.
    logger.debug(
        "Prompt cache skipped for %s: prompt %d chars < minimum %d",
        model_name, len(plain), min_chars,
    )
    return None


def _cache_blocks_for(
    provider: str,
    bedrock_native: bool,
    model_name: str,
    static_text: str,
    dynamic_text: str,
    plain: str,
) -> Optional[Tuple[List[dict], str]]:
    """``(system blocks, wire-format label)`` for the serving provider, or ``None``
    when the prompt must stay a plain string (unknown provider, explicit
    ``bedrock/`` id through the gateway, prompt below the cacheable minimum)."""
    min_chars = min_cacheable_chars(model_name)
    if provider == "bedrock" and bedrock_native:
        return _bedrock_native_blocks(model_name, static_text, dynamic_text, min_chars)
    anthropic_style = provider in _ANTHROPIC_STYLE_PROVIDERS or (
        provider == "bedrock" and _has_anthropic_prefix(model_name)
    )
    if anthropic_style:
        # Anthropic Messages API semantics: direct, OpenRouter, or the Bedrock
        # gateway for the anthropic/ ids it already received markers for.
        return _anthropic_style_blocks(model_name, static_text, dynamic_text, plain, min_chars)
    return None


def build_cached_system_prompt(
    model_name: str,
    static_text: str,
    dynamic_text: str = "",
    *,
    serving_provider: Optional[str] = None,
    bedrock_native: bool = False,
) -> "str | SystemMessage":
    """Return the system prompt for ``create_agent`` with a cache breakpoint applied.

    Args:
        model_name: Aurora model id (``anthropic/claude-opus-5.5``,
            ``bedrock/eu.anthropic.claude-haiku-5-5``, ...).
        static_text: Cacheable prefix (see ``assemble_system_prompt_parts``).
        dynamic_text: Per-session/per-message suffix; may be empty.
        serving_provider: Registry name of the provider that serves the call
            (``get_registry().resolve_provider_name(...)``): ``bedrock``, ``anthropic``,
            ``openrouter``, ``google``, ... Decides the wire format.
        bedrock_native: True when the Bedrock provider runs in native (Converse SDK)
            mode rather than through an OpenAI-compatible gateway.

    Returns a ``SystemMessage`` whose content is a list of blocks with the
    breakpoint right after ``static_text`` when the model is Claude-family, the
    serving provider accepts cache markers and the static prefix reaches the
    model's cacheable minimum. On the Anthropic-style paths a prompt whose static
    part is too short but whose whole text is long enough gets the single
    end-of-system breakpoint instead. Otherwise returns the plain joined string,
    so non-Claude models, unknown providers, the explicit-``bedrock/`` gateway
    path and tiny prompts behave exactly as before caching existed.

    Caching can never break a session: on any unexpected failure the plain string
    is returned.
    """
    static_text = static_text if isinstance(static_text, str) else ""
    dynamic_text = dynamic_text if isinstance(dynamic_text, str) else ""
    plain = _plain(static_text, dynamic_text)
    if not static_text or not is_claude_model(model_name):
        return plain

    provider = (serving_provider or "").strip().lower()

    try:
        choice = _cache_blocks_for(provider, bedrock_native, model_name, static_text, dynamic_text, plain)
        if choice is None:
            return plain
        blocks, wire = choice
        msg = SystemMessage(content=blocks)
        logger.info(
            "Applying prompt cache breakpoint (%s, provider=%s, ttl=5m, static_chars=%d, dynamic_chars=%d)",
            wire, provider, len(static_text), len(dynamic_text),
        )
        return msg
    except Exception:
        # Never let caching break the agent: degrade to the uncached prompt.
        logger.warning(
            "Failed to build cached system prompt; falling back to plain prompt",
            exc_info=True,
        )
        return plain
