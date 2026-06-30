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

from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)

# Smallest cacheable prefix is model-specific (Sonnet/Opus 4.8 = 1024 tokens,
# Opus 4.5/4.6 & Haiku 4.5 = 4096). We gate on characters, not tokens, to stay
# import-light. ~4 chars/token, and we use the largest minimum (4096 tokens) so
# we never waste a cache WRITE on a prompt too small to cache on any model.
# ponytail: char heuristic; upgrade path = count tokens via the model tokenizer.
_MIN_PREFIX_CHARS = 4096 * 4


def _caching_enabled() -> bool:
    # Default OFF: opt in per environment (staging first, then prod) after
    # confirming cache_read tokens appear. Enable with PROMPT_CACHING_ENABLED=true.
    return os.getenv("PROMPT_CACHING_ENABLED", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _is_anthropic_model(model_name: str) -> bool:
    if not model_name:
        return False
    # OpenAI/Gemini auto-cache server-side and ignore (or reject) cache_control,
    # so we only tag Anthropic-family models. Match the OpenRouter id prefix.
    return model_name.split("/", 1)[0].lower() == "anthropic"


def build_cached_system_prompt(
    model_name: str, system_prompt_text: str
) -> "str | SystemMessage":
    """Return a system prompt for ``create_agent`` with Anthropic caching applied.

    Returns a ``SystemMessage`` carrying a ``cache_control`` ephemeral breakpoint
    when caching is enabled, the model is Anthropic-family, and the prompt is
    large enough to be cacheable. Otherwise returns the original string unchanged
    (no-op), so non-Anthropic models (Google/OpenAI/etc.) and tiny prompts are
    completely unaffected and behave exactly as before.

    The breakpoint uses the default 5-minute ephemeral TTL (no explicit ``ttl``
    field). Prod data: 99.9% of inter-turn gaps are <5min and the TTL refreshes
    on every read, so 5m hits ~all turns; a 1h TTL costs 2x to write to rescue
    ~0.05% of turns, so it is not worth exposing as a knob.

    Caching can never break a session: on any unexpected failure we fall back to
    the plain string, which is the original (pre-caching) behavior.
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

    try:
        msg = SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": system_prompt_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
        logger.info(
            "Applying Anthropic prompt cache breakpoint (ttl=5m, prefix_chars=%d)",
            len(system_prompt_text),
        )
        return msg
    except Exception:
        # Never let caching break the agent: degrade to the uncached prompt.
        logger.warning(
            "Failed to build cached system prompt; falling back to plain prompt",
            exc_info=True,
        )
        return system_prompt_text
