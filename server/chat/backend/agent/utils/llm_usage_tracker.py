import logging
import os
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass
import tiktoken
import json
from langchain_core.callbacks import BaseCallbackHandler
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from chat.backend.agent.call_options import claude_catalog_ids
from .openrouter_pricing_service import get_pricing_service
from .provider_pricing_service import get_provider_pricing_service

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Data class for LLM usage tracking.

    ``input_tokens`` is the request's WHOLE prompt (LangChain's ``usage_metadata``
    convention: non-cached input + cache reads + cache writes), so the generated
    ``total_tokens`` column keeps meaning "tokens processed". The two cache
    columns break that prompt down: ``cache_read_input_tokens`` were served from
    the prompt cache (billed at the discounted read rate) and
    ``cache_write_input_tokens`` were written to it (billed at the write rate);
    the remainder was billed at the full input rate.
    """

    user_id: str
    session_id: Optional[str]
    model_name: str
    api_provider: str
    request_type: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    response_time_ms: int
    org_id: Optional[str] = None
    error_message: Optional[str] = None
    request_metadata: Optional[Dict[str, Any]] = None
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0


# Cross-region premium Bedrock charges for geo CRIS inference profiles (``us.`` /
# ``eu.`` / ``apac.`` …) relative to the direct / ``global.`` list price. AWS publishes
# ~10%; the real figure is on the customer's Bedrock invoice, so it is configurable
# (``BEDROCK_REGIONAL_PREMIUM``) and should be calibrated against Cost Explorer.
_DEFAULT_REGIONAL_PREMIUM = 1.10


def _bedrock_regional_premium() -> float:
    """``BEDROCK_REGIONAL_PREMIUM`` as a multiplier; unset / empty / junk / < 1.0 → 1.10.

    Read on every call so an operator can change it without a restart-sensitive
    import-time constant (and so tests can monkeypatch the environment). A value
    of exactly ``1.0`` disables the premium.
    """
    raw = (os.getenv("BEDROCK_REGIONAL_PREMIUM") or "").strip()
    if not raw:
        return _DEFAULT_REGIONAL_PREMIUM
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_REGIONAL_PREMIUM
    return value if value >= 1.0 else _DEFAULT_REGIONAL_PREMIUM


def resolve_serving_provider(model_name: str, provider_mode: Optional[str] = None) -> str:
    """Name of the provider that actually serves ``model_name`` in this deployment.

    Routes through the provider registry exactly like ``create_chat_model`` does, so
    a clean ``anthropic/`` id under ``LLM_PROVIDER_MODE=bedrock`` is labelled
    ``bedrock`` (that is who bills it), not ``anthropic``. Never raises: when the
    registry cannot resolve the model (provider unavailable, unknown id) it falls
    back to the id prefix under direct routing and to the mode name otherwise.
    """
    mode = (provider_mode or os.getenv("LLM_PROVIDER_MODE") or "direct").strip().lower()
    try:
        from chat.backend.agent.providers import get_registry

        resolved = get_registry().resolve_provider_name(model_name, mode)
        if resolved:
            return resolved
    except Exception:
        logger.debug("Could not resolve the serving provider for %s", model_name, exc_info=True)
    if mode in ("direct", "auto"):
        try:
            from chat.backend.agent.model_mapper import ModelMapper

            return ModelMapper.detect_provider(model_name) or mode
        except Exception:
            return mode
    return mode


# Two-tier pricing row (per 1K tokens). Anthropic bills Claude Haiku 5.5 by prompt
# length: <= 100K prompt tokens at the base rates, > 100K at the `long_context` rates
# — for input, output, cache reads and cache writes alike. Both spellings of the id
# share this one object so the tiers cannot drift apart.
_HAIKU_5_5_PRICING = {
    "input": 0.0001,
    "output": 0.0005,
    "cached_input": 0.00001,
    "cache_write": 0.000125,
    "long_context": {
        "threshold_tokens": 100000,
        "input": 0.0005,
        "output": 0.0025,
        "cached_input": 0.00005,
        "cache_write": 0.000625,
    },
}


class LLMUsageTracker:
    """Tracks LLM usage including token counting and cost calculation"""

    MODEL_PRICING = {
        # OpenAI (direct API pricing per 1K tokens)
        # cached_input = automatic prompt caching discount
        "openai/gpt-6-astra": {"input": 0.010, "output": 0.050, "cached_input": 0.001},
        "openai/gpt-5.6-sol": {"input": 0.004, "output": 0.020, "cached_input": 0.0004},
        "openai/gpt-5.6-terra": {"input": 0.002, "output": 0.012, "cached_input": 0.0002},
        "openai/gpt-5.6-luna": {"input": 0.0002, "output": 0.0012, "cached_input": 0.00002},
        "openai/gpt-5.5": {"input": 0.005, "output": 0.030, "cached_input": 0.00125},
        "openai/gpt-5.4": {"input": 0.0025, "output": 0.015, "cached_input": 0.000625},
        "openai/gpt-5.2": {"input": 0.00175, "output": 0.014, "cached_input": 0.0004375},
        "openai/o3": {"input": 0.002, "output": 0.008, "cached_input": 0.0005},
        "openai/o3-mini": {"input": 0.0011, "output": 0.0044, "cached_input": 0.000275},
        "openai/o4-mini": {"input": 0.0011, "output": 0.0044, "cached_input": 0.000275},
        "openai/gpt-4.1": {"input": 0.002, "output": 0.008, "cached_input": 0.0005},
        "openai/gpt-4.1-mini": {"input": 0.0004, "output": 0.0016, "cached_input": 0.0001},
        "openai/gpt-4o": {"input": 0.0025, "output": 0.01, "cached_input": 0.00125},
        "openai/gpt-4o-mini": {"input": 0.00015, "output": 0.0006, "cached_input": 0.000075},
        # Anthropic (cached input = 90% discount; 95% on Opus 5.5 / Sonnet 5.5, 97.5% on Fable 5.1).
        # cache_write = 5-minute cache write rate (1.25x input). Rates per 1K tokens, read off
        # platform.claude.com/docs/en/about-claude/pricing on 2026-10-22 (Sonnet 5.5, Opus 5
        # and Opus 4.8 re-read the same day). Bedrock-served ids (``bedrock/eu.anthropic.…``,
        # bare ``eu.anthropic.…``, dated ``…-20251001-v1:0``) resolve to these rows through
        # ``_pricing_key``; Bedrock's own Anthropic price table is rendered dynamically and
        # could not be read, so geo profiles bill these list prices times the configurable
        # ``BEDROCK_REGIONAL_PREMIUM``.
        "anthropic/claude-opus-5.5": {"input": 0.004, "output": 0.020, "cached_input": 0.0002, "cache_write": 0.005},
        "anthropic/claude-opus-5-5": {"input": 0.004, "output": 0.020, "cached_input": 0.0002, "cache_write": 0.005},
        # Sonnet 5.5 and Opus 5 / 4.8 are not in the picker; priced so Bedrock rows that
        # name them (operator overrides, RCA_ORCHESTRATOR_MODEL) do not fall to `default`.
        "anthropic/claude-sonnet-5.5": {"input": 0.002, "output": 0.010, "cached_input": 0.0001, "cache_write": 0.0025},
        "anthropic/claude-sonnet-5-5": {"input": 0.002, "output": 0.010, "cached_input": 0.0001, "cache_write": 0.0025},
        "anthropic/claude-sonnet-5": {"input": 0.002, "output": 0.010, "cached_input": 0.0002, "cache_write": 0.0025},
        # Claude Haiku 5.5 (cheap tier) is priced by prompt length: a request whose prompt
        # (all input tokens, cache reads and writes included) exceeds 100K tokens bills the
        # `long_context` row for every token of that request. See _resolve_price_tier().
        "anthropic/claude-haiku-5.5": _HAIKU_5_5_PRICING,
        "anthropic/claude-haiku-5-5": _HAIKU_5_5_PRICING,
        "anthropic/claude-fable-5.1": {"input": 0.010, "output": 0.050, "cached_input": 0.00025, "cache_write": 0.0125},
        "anthropic/claude-fable-5-1": {"input": 0.010, "output": 0.050, "cached_input": 0.00025, "cache_write": 0.0125},
        "anthropic/claude-fable-5": {"input": 0.010, "output": 0.050, "cached_input": 0.001, "cache_write": 0.0125},
        "anthropic/claude-opus-5": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4.8": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4-8": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4.7": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4-7": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4.6": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4-6": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-sonnet-4.6": {"input": 0.003, "output": 0.015, "cached_input": 0.0003, "cache_write": 0.00375},
        "anthropic/claude-sonnet-4-6": {"input": 0.003, "output": 0.015, "cached_input": 0.0003, "cache_write": 0.00375},
        "anthropic/claude-opus-4.5": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-opus-4-5": {"input": 0.005, "output": 0.025, "cached_input": 0.0005, "cache_write": 0.00625},
        "anthropic/claude-sonnet-4.5": {"input": 0.003, "output": 0.015, "cached_input": 0.0003, "cache_write": 0.00375},
        "anthropic/claude-sonnet-4-5": {"input": 0.003, "output": 0.015, "cached_input": 0.0003, "cache_write": 0.00375},
        "anthropic/claude-haiku-4.5": {"input": 0.001, "output": 0.005, "cached_input": 0.0001, "cache_write": 0.00125},
        "anthropic/claude-haiku-4-5": {"input": 0.001, "output": 0.005, "cached_input": 0.0001, "cache_write": 0.00125},
        "anthropic/claude-3.5-sonnet": {"input": 0.003, "output": 0.015, "cached_input": 0.0003, "cache_write": 0.00375},
        "anthropic/claude-3-haiku": {"input": 0.00025, "output": 0.00125, "cached_input": 0.000025, "cache_write": 0.0003125},
        # Google AI / Vertex AI (cached input = 75% discount, except Argon at 95%)
        # Argon intro rates through 2026-11-30; $4/$20 per 1M after that.
        "google/gemini-4-argon": {"input": 0.002, "output": 0.010, "cached_input": 0.0001},
        "google/gemini-3.8-flash": {"input": 0.00075, "output": 0.00375, "cached_input": 0.000075},
        "google/gemini-3.6-flash": {"input": 0.00075, "output": 0.00375, "cached_input": 0.000075},
        "google/gemini-3.5-flash-lite": {"input": 0.0003, "output": 0.0025, "cached_input": 0.00003},
        "google/gemini-3.5-flash": {"input": 0.0015, "output": 0.009, "cached_input": 0.00015},
        "google/gemini-3.1-pro-preview": {"input": 0.002, "output": 0.012, "cached_input": 0.0005},
        "google/gemini-3.1-flash-lite-preview": {"input": 0.00025, "output": 0.0015, "cached_input": 0.0000625},
        "google/gemini-3-flash": {"input": 0.0005, "output": 0.003, "cached_input": 0.000125},
        "google/gemini-2.5-pro": {"input": 0.00125, "output": 0.01, "cached_input": 0.0003125},
        "google/gemini-2.5-flash": {"input": 0.0003, "output": 0.0025, "cached_input": 0.000075},
        "google/gemini-2.5-flash-lite": {"input": 0.0001, "output": 0.0004, "cached_input": 0.000025},
        "vertex/gemini-4-argon": {"input": 0.002, "output": 0.010, "cached_input": 0.0001},
        "vertex/gemini-3.8-flash": {"input": 0.00075, "output": 0.00375, "cached_input": 0.000075},
        "vertex/gemini-3.6-flash": {"input": 0.00075, "output": 0.00375, "cached_input": 0.000075},
        "vertex/gemini-3.5-flash-lite": {"input": 0.0003, "output": 0.0025, "cached_input": 0.00003},
        "vertex/gemini-3.5-flash": {"input": 0.0015, "output": 0.009, "cached_input": 0.00015},
        "vertex/gemini-3.1-pro-preview": {"input": 0.002, "output": 0.012, "cached_input": 0.0005},
        "vertex/gemini-3.1-flash-lite-preview": {"input": 0.00025, "output": 0.0015, "cached_input": 0.0000625},
        "vertex/gemini-3-flash": {"input": 0.0005, "output": 0.003, "cached_input": 0.000125},
        "vertex/gemini-2.5-pro": {"input": 0.00125, "output": 0.01, "cached_input": 0.0003125},
        "vertex/gemini-2.5-flash": {"input": 0.0003, "output": 0.0025, "cached_input": 0.000075},
        "vertex/gemini-2.5-flash-lite": {"input": 0.0001, "output": 0.0004, "cached_input": 0.000025},
        # Ollama (local, free)
        "ollama/llama3.1": {"input": 0.0, "output": 0.0},
        "ollama/qwen2.5": {"input": 0.0, "output": 0.0},
        # Default fallback
        "default": {"input": 0.001, "output": 0.002},
    }

    @classmethod
    def count_tokens(cls, text: str, model_name: str = "gpt-4") -> int:
        """Count tokens in text using tiktoken"""
        try:
            if "gpt-4o" in model_name:
                encoding_name = "o200k_base"
            elif "gpt-4" in model_name or "gpt-3.5" in model_name:
                encoding_name = "cl100k_base"
            else:
                encoding_name = "cl100k_base"

            encoding = tiktoken.get_encoding(encoding_name)
            return len(encoding.encode(str(text)))

        except Exception as e:
            logger.warning(
                f"Error counting tokens: {e}. Using character-based estimation."
            )
            # Fallback: rough estimation (1 token ≈ 4 characters)
            return len(str(text)) // 4

    @classmethod
    def count_tokens_from_messages(
        cls, messages: Any, model_name: str = "gpt-4"
    ) -> int:
        """Count tokens from message objects"""
        try:
            total_tokens = 0

            if isinstance(messages, list):
                for message in messages:
                    if hasattr(message, "content"):
                        # Handle multimodal content
                        if isinstance(message.content, list):
                            for content_part in message.content:
                                if isinstance(content_part, dict):
                                    if content_part.get("type") == "text":
                                        total_tokens += cls.count_tokens(
                                            content_part.get("text", ""), model_name
                                        )
                                    elif content_part.get("type") == "image_url":
                                        # Images roughly cost 85 tokens per image for vision models
                                        total_tokens += 85
                                elif isinstance(content_part, str):
                                    total_tokens += cls.count_tokens(
                                        content_part, model_name
                                    )
                        else:
                            total_tokens += cls.count_tokens(
                                str(message.content), model_name
                            )
                    elif hasattr(message, "text"):
                        total_tokens += cls.count_tokens(message.text, model_name)
                    else:
                        total_tokens += cls.count_tokens(str(message), model_name)
            else:
                total_tokens = cls.count_tokens(str(messages), model_name)

            return total_tokens

        except Exception as e:
            logger.warning(f"Error counting tokens from messages: {e}")
            return cls.count_tokens(str(messages), model_name)

    # Cross-region premium charged by Vertex non-global endpoints (and the default for
    # Bedrock geo CRIS profiles), relative to the global/direct list price. AWS + Google
    # both publish ~10%. The Bedrock figure is configurable: BEDROCK_REGIONAL_PREMIUM.
    _REGIONAL_PREMIUM_MULTIPLIER = _DEFAULT_REGIONAL_PREMIUM

    @classmethod
    def _pricing_key(cls, model_name: str) -> Optional[str]:
        """Static ``MODEL_PRICING`` key for any spelling of a model id, or ``None``.

        Tried in order: the id itself; for Claude models the catalog ids derived by
        ``call_options.claude_catalog_ids`` (strips ``bedrock/``, the geo prefix, the
        ``anthropic.`` / ``anthropic/`` vendor prefix and dated ``-20251001-v1:0``
        suffixes; dashed and dotted version spellings); finally the legacy
        ``split(".")[0].split("-v")[0]`` heuristic. So
        ``bedrock/eu.anthropic.claude-opus-5-5``, ``eu.anthropic.claude-haiku-5-5`` and
        ``anthropic.claude-haiku-4-5-20251001-v1:0`` all price as their Anthropic row
        instead of the ``default`` $1/$2 rate.
        """
        if not model_name:
            return None
        table = cls.MODEL_PRICING
        if model_name in table:
            return model_name
        for candidate in claude_catalog_ids(model_name):
            if candidate in table:
                return candidate
        base_model = model_name.split(".")[0].split("-v")[0]
        if base_model in table:
            return base_model
        return None

    @classmethod
    def _resolve_regional_premium(cls, provider_mode: Optional[str], model_name: str) -> float:
        """Return the price multiplier for the deployment's endpoint (1.0 = no premium).

        Only regional endpoints carry a premium; global endpoints bill at the direct rate.
        """
        mode = (provider_mode or "").lower()
        lowered = (model_name or "").lower()

        # An explicit ``bedrock/<id>`` is served by Bedrock whatever the routing mode
        # says (LLM_PROVIDER_MODE=direct with pinned ids is the documented gateway /
        # pinning setup), so it carries the Bedrock premium too.
        if mode != "bedrock" and lowered.startswith("bedrock/"):
            mode = "bedrock"

        # Bedrock: Aurora routes through geo CRIS (us./eu./apac.) which is ~10% over
        # global (BEDROCK_REGIONAL_PREMIUM, default 1.10). Region falls back the same
        # way the Bedrock provider resolves it. An explicit global profile (region
        # "global", or a pinned ``bedrock/global.…`` id) bills at the direct rate.
        if mode == "bedrock":
            if lowered.startswith("bedrock/global."):
                return 1.0
            region = (
                os.getenv("BEDROCK_REGION")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or ""
            ).lower()
            if region == "global":
                return 1.0
            return _bedrock_regional_premium()

        # Vertex: non-global (regional/EU) endpoints are +10%. The provider defaults to
        # the global endpoint (VERTEX_AI_LOCATION=global), which bills at the direct rate.
        if mode == "vertex" and "vertex" in model_name.lower():
            location = os.getenv("VERTEX_AI_LOCATION", "global").lower()
            if location and location != "global":
                return cls._REGIONAL_PREMIUM_MULTIPLIER
            return 1.0

        # Direct / OpenRouter / everything else: no cloud-reseller premium.
        return 1.0

    @classmethod
    def _resolve_price_tier(cls, pricing: Dict[str, Any], prompt_tokens: int) -> Dict[str, float]:
        """Collapse a two-tier pricing row to the flat rates that apply to one request.

        Rows may carry a ``long_context`` sub-table (``threshold_tokens`` + rates). When
        the request's prompt length exceeds the threshold, every rate of that request
        comes from the sub-table (Anthropic prices the whole request, not just the
        tokens past the threshold). Flat rows are returned unchanged minus the key, so
        callers always see numeric values only.
        """
        tier = pricing.get("long_context")
        flat = {k: v for k, v in pricing.items() if k != "long_context"}
        if isinstance(tier, dict):
            threshold = tier.get("threshold_tokens")
            if isinstance(threshold, (int, float)) and prompt_tokens > threshold:
                flat.update({k: v for k, v in tier.items() if k != "threshold_tokens"})
        return flat

    @classmethod
    def calculate_cost(
        cls,
        input_tokens: int,
        output_tokens: int,
        model_name: str,
        use_dynamic_pricing: bool = True,
        provider_mode: Optional[str] = None,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Calculate estimated cost based on token usage and model pricing.

        Pricing resolution order:
        1. OpenRouter mode -> OpenRouter dynamic pricing API
        2. Direct mode, Google/Vertex models -> Google Cloud Billing Catalog API
        3. Static MODEL_PRICING table (all providers, always available; any spelling
           of a Claude id — ``bedrock/eu.anthropic.…``, dated Bedrock ids — resolves
           to its row through ``_pricing_key``)
        4. Default fallback

        ``input_tokens`` is the request's WHOLE prompt (LangChain's ``usage_metadata``
        convention — Bedrock's raw ``inputTokens`` excludes cache tokens, langchain-aws
        adds them back). ``cached_input_tokens`` of it were read from the prompt cache
        and ``cache_write_tokens`` were written to it; the remainder is billed at the
        full input rate, cache reads at ``cached_input`` and cache writes at
        ``cache_write`` (1.25x input when a row has no explicit write rate — the
        Anthropic 5-minute write multiplier). Two-tier rows (Haiku 5.5) pick their tier
        by the whole prompt, cache tokens included, as Anthropic bills them.

        Platform surcharge: Bedrock and Vertex resell Anthropic/Google models at
        the same rate as direct ONLY on their global endpoints. Aurora's Bedrock
        provider routes through *regional* cross-region inference profiles
        (us./eu./apac.), and Vertex non-global endpoints are billed at a ~10%
        premium over the global/direct list price. When the deployment is on such
        a regional endpoint we scale the static rate up by that premium so billing
        matches the customer's actual cloud invoice.
        """
        if provider_mode is None:
            provider_mode = os.getenv("LLM_PROVIDER_MODE")

        try:
            cached_input_tokens = max(int(cached_input_tokens or 0), 0)
            cache_write_tokens = max(int(cache_write_tokens or 0), 0)

            pricing = None
            # The regional premium is baked into the static list price only; dynamic
            # OpenRouter / GCP-billing rates already reflect the real per-endpoint price.
            using_static_pricing = False

            if use_dynamic_pricing:
                if provider_mode == "openrouter":
                    try:
                        pricing_service = get_pricing_service()
                        pricing = pricing_service.get_model_pricing(model_name)
                        logger.debug(f"Using OpenRouter dynamic pricing for {model_name}: {pricing}")
                    except Exception as e:
                        logger.warning(
                            f"Failed to get OpenRouter dynamic pricing for {model_name}: {e}"
                        )
                elif any(kw in model_name.lower() for kw in ("gemini", "google", "vertex")):
                    # Only call GCP Billing Catalog API for Google/Gemini models
                    try:
                        provider_svc = get_provider_pricing_service()
                        pricing = provider_svc.get_model_pricing(model_name)
                        if pricing:
                            logger.debug(f"Using provider billing API pricing for {model_name}: {pricing}")
                    except Exception as e:
                        logger.debug(
                            f"Provider billing API unavailable for {model_name}: {e}"
                        )

            if not pricing:
                using_static_pricing = True
                pricing_key = cls._pricing_key(model_name)
                pricing = cls.MODEL_PRICING.get(pricing_key) if pricing_key else None

                if not pricing:
                    pricing = cls.MODEL_PRICING["default"]
                    logger.info(
                        f"Using default static pricing for unknown model: {model_name}"
                    )
                else:
                    logger.debug(f"Using static pricing for {model_name} ({pricing_key}): {pricing}")

            # Regional cloud endpoints (Bedrock geo CRIS, Vertex non-global) bill ~10%
            # above the global/direct list price baked into the static table. Only adjust
            # the static path: the OpenRouter/GCP-billing dynamic paths already return the
            # real per-endpoint rate. A global endpoint (BEDROCK_REGION=global) resolves to
            # 1.0, so the `> 1.0` guard skips it without an exact float equality check.
            # Two-tier rows (Haiku 5.5) pick their rates by prompt length first, so the
            # premium below only ever sees flat numeric rates. By this function's contract
            # `input_tokens` is the request's whole prompt (cache reads and writes
            # included); the max() guards callers that pass only the non-cached remainder
            # alongside larger cache counts.
            prompt_tokens = max(input_tokens, cached_input_tokens + cache_write_tokens)
            pricing = cls._resolve_price_tier(pricing, prompt_tokens)

            premium = cls._resolve_regional_premium(provider_mode, model_name)
            if using_static_pricing and premium > 1.0:
                pricing = {
                    k: (v * premium if isinstance(v, (int, float)) else v)
                    for k, v in pricing.items()
                }
                logger.debug(
                    f"Applied {premium:.2f}x regional endpoint premium for {model_name} ({provider_mode})"
                )

            input_rate = pricing["input"]
            cached_rate = pricing.get("cached_input", input_rate)
            # 5-minute cache writes cost 1.25x the input rate on every Claude model;
            # rows from the dynamic pricing services carry no write rate.
            cache_write_rate = pricing.get("cache_write", input_rate * 1.25)

            non_cached_input = max(input_tokens - cached_input_tokens - cache_write_tokens, 0)
            input_cost = (non_cached_input / 1000) * input_rate
            input_cost += (cached_input_tokens / 1000) * cached_rate
            input_cost += (cache_write_tokens / 1000) * cache_write_rate
            output_cost = (output_tokens / 1000) * pricing["output"]

            return round(input_cost + output_cost, 6)

        except Exception as e:
            logger.warning(f"Error calculating cost: {e}")
            return 0.0

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def extract_token_counts(cls, usage_metadata: Any) -> Dict[str, int]:
        """Token counts from a LangChain ``usage_metadata`` (or OpenAI-style usage) dict.

        Returns ``input_tokens`` (whole prompt), ``output_tokens``,
        ``cached_input_tokens`` (prompt-cache reads) and ``cache_write_tokens``
        (prompt-cache writes). Cache writes are reported two ways: a total under
        ``input_token_details.cache_creation``, or per TTL under
        ``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens`` — langchain-aws
        1.9 and langchain-anthropic both zero ``cache_creation`` when they emit the
        per-TTL breakdown, so the two conventions are reconciled with ``max`` (never
        summed, which would double count a provider that reports both in full).
        OpenAI-style dicts (``prompt_tokens`` / ``completion_tokens`` /
        ``prompt_tokens_details.cached_tokens``) are accepted as well. Missing or
        malformed fields read as 0.
        """
        counts = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
        }
        if not isinstance(usage_metadata, dict):
            return counts
        counts["input_tokens"] = cls._int_or_zero(
            usage_metadata.get("prompt_tokens", usage_metadata.get("input_tokens", 0))
        )
        counts["output_tokens"] = cls._int_or_zero(
            usage_metadata.get("completion_tokens", usage_metadata.get("output_tokens", 0))
        )
        details = usage_metadata.get("input_token_details")
        if not isinstance(details, dict):
            details = usage_metadata.get("prompt_tokens_details")
        if isinstance(details, dict):
            counts["cached_input_tokens"] = cls._int_or_zero(
                details.get("cache_read", details.get("cached_tokens", 0))
            )
            cache_creation = cls._int_or_zero(details.get("cache_creation", 0))
            per_ttl = cls._int_or_zero(details.get("ephemeral_5m_input_tokens", 0)) + cls._int_or_zero(
                details.get("ephemeral_1h_input_tokens", 0)
            )
            counts["cache_write_tokens"] = max(cache_creation, per_ttl)
        return counts

    @classmethod
    def extract_usage_from_response(cls, response: Any) -> Dict[str, int]:
        """Extract token usage from API response when available"""
        empty = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
        }
        try:
            usage = dict(empty)

            # Try to extract from various response formats
            # 1 LangChain messages: usage_metadata dict (cache read / write details included)
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = cls.extract_token_counts(response.usage_metadata)

            # 2 Traditional attribute-based usage object
            elif hasattr(response, "usage"):
                if hasattr(response.usage, "prompt_tokens"):
                    usage["input_tokens"] = response.usage.prompt_tokens
                if hasattr(response.usage, "completion_tokens"):
                    usage["output_tokens"] = response.usage.completion_tokens
                if hasattr(response.usage, "prompt_tokens_details"):
                    details = response.usage.prompt_tokens_details
                    if hasattr(details, "cached_tokens"):
                        usage["cached_input_tokens"] = details.cached_tokens

            # 3 Dict-based responses (e.g. OpenAI v1 chat API style)
            elif isinstance(response, dict):
                # Prefer nested usage_metadata first
                if "usage_metadata" in response and response["usage_metadata"]:
                    usage = cls.extract_token_counts(response["usage_metadata"])
                elif "usage" in response:
                    usage = cls.extract_token_counts(response["usage"])

            return usage

        except Exception as e:
            logger.warning(f"Error extracting usage from response: {e}")
            return dict(empty)

    @classmethod
    def store_usage(cls, usage: LLMUsage) -> bool:
        """Store LLM usage data in the database"""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()

                resolved_org_id = set_rls_context(cursor, conn, usage.user_id, log_prefix="[LLMUsage:store]")
                if not resolved_org_id:
                    logger.error("[LLMUsage:store] Cannot store usage — org_id unresolvable for user %s", usage.user_id)
                    return False
                usage.org_id = resolved_org_id

                # Insert usage record. The cache columns are added by the idempotent
                # migration in utils/db/db_utils.py (ALTER TABLE ... IF NOT EXISTS).
                cursor.execute(
                    """
                    INSERT INTO llm_usage_tracking (
                        user_id, org_id, session_id, model_name, api_provider, request_type,
                        input_tokens, output_tokens, estimated_cost, response_time_ms,
                        error_message, request_metadata,
                        cache_read_input_tokens, cache_write_input_tokens
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        usage.user_id,
                        usage.org_id,
                        usage.session_id,
                        usage.model_name,
                        usage.api_provider,
                        usage.request_type,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.estimated_cost,
                        usage.response_time_ms,
                        usage.error_message,
                        json.dumps(usage.request_metadata)
                        if usage.request_metadata
                        else None,
                        max(int(usage.cache_read_input_tokens or 0), 0),
                        max(int(usage.cache_write_input_tokens or 0), 0),
                    ),
                )

                conn.commit()
                cache_note = ""
                if usage.cache_read_input_tokens or usage.cache_write_input_tokens:
                    cache_note = (
                        f" (cache read {usage.cache_read_input_tokens}, "
                        f"write {usage.cache_write_input_tokens})"
                    )
                logger.info(
                    f"Stored LLM usage: {usage.model_name} - {usage.input_tokens}+{usage.output_tokens} tokens"
                    f"{cache_note} - ${usage.estimated_cost:.6f}"
                )
                
                # Clear API cost cache for this user to force refresh on next cost check
                try:
                    from utils.billing.billing_cache import clear_user_cache

                    clear_user_cache(usage.user_id)
                    logger.debug(f"Cleared API cost cache for user {usage.user_id} after new usage record")
                except Exception as cache_error:
                    logger.warning(f"Failed to clear API cost cache after usage store: {cache_error}")

                # Report usage to marketplace metering (no-op unless hooks are configured).
                # Implementations must be non-blocking (buffer + flush pattern).
                try:
                    from utils.hooks import get_hook
                    get_hook("report_usage")(usage.org_id, usage.estimated_cost, {
                        "model": usage.model_name,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                        "cache_write_input_tokens": usage.cache_write_input_tokens,
                    })
                except Exception as hook_err:
                    logger.debug("report_usage hook error (non-fatal): %s", hook_err)

                return True

        except Exception as e:
            logger.error(f"Error storing LLM usage: {e}")
            return False

    @classmethod
    def track_llm_call(
        cls,
        user_id: str,
        session_id: Optional[str],
        model_name: str,
        request_type: str,
        prompt: Any,
        response: Any = None,
        start_time: Optional[float] = None,
        error_message: Optional[str] = None,
        api_provider: Optional[str] = None,
        provider_mode: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> bool:
        """
        Track a complete LLM API call with token counting and cost calculation

        Args:
            user_id: User identifier
            session_id: Chat session identifier (optional)
            model_name: Name of the model used
            request_type: Type of request (classify_query, generate_sql, etc.)
            prompt: Input prompt/messages
            response: API response (optional)
            start_time: Request start time for calculating response time
            error_message: Error message if request failed
            api_provider: Provider that served the call. Defaults to the one the
                registry resolves for ``model_name`` (``resolve_serving_provider``),
                so a Claude call under ``LLM_PROVIDER_MODE=bedrock`` is labelled
                ``bedrock``, not the id prefix or a hardcoded name.
            provider_mode: Pricing mode. Defaults to ``api_provider`` (same contract
                as ``tracked_invoke``), so a Bedrock-served row carries the regional
                premium whatever the global routing mode is.
        """
        try:
            if not api_provider:
                api_provider = resolve_serving_provider(model_name, provider_mode)
            if provider_mode is None:
                provider_mode = api_provider

            # Count input tokens
            input_tokens = cls.count_tokens_from_messages(prompt, model_name)

            # Count output tokens
            output_tokens = 0
            cached_input_tokens = 0
            cache_write_tokens = 0
            if response:
                # Try to extract from API response first
                usage_data = cls.extract_usage_from_response(response)
                if usage_data["input_tokens"] > 0:
                    input_tokens = usage_data["input_tokens"]
                cached_input_tokens = usage_data.get("cached_input_tokens", 0)
                cache_write_tokens = usage_data.get("cache_write_tokens", 0)
                if usage_data["output_tokens"] > 0:
                    output_tokens = usage_data["output_tokens"]
                else:
                    # Fallback to counting response text
                    if hasattr(response, "content"):
                        output_tokens = cls.count_tokens(
                            str(response.content), model_name
                        )
                    elif isinstance(response, dict) and "content" in response:
                        output_tokens = cls.count_tokens(
                            str(response["content"]), model_name
                        )
                    else:
                        output_tokens = cls.count_tokens(str(response), model_name)

            # Calculate cost
            estimated_cost = cls.calculate_cost(
                input_tokens, output_tokens, model_name,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
                provider_mode=provider_mode,
            )

            # Calculate response time
            response_time_ms = (
                int((time.time() - start_time) * 1000) if start_time else 0
            )

            # Create usage record
            usage = LLMUsage(
                user_id=user_id,
                session_id=session_id,
                model_name=model_name,
                api_provider=api_provider,
                request_type=request_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                response_time_ms=response_time_ms,
                org_id=org_id,
                error_message=error_message,
                request_metadata={
                    "has_images": cls._has_image_content(prompt),
                    "message_count": len(prompt) if isinstance(prompt, list) else 1,
                },
                cache_read_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_tokens,
            )

            # Store in database
            return cls.store_usage(usage)

        except Exception as e:
            logger.error(f"Error tracking LLM call: {e}")
            return False

    @classmethod
    def _has_image_content(cls, prompt: Any) -> bool:
        """Check if the prompt contains image content"""
        try:
            if isinstance(prompt, list):
                for message in prompt:
                    if hasattr(message, "content") and isinstance(
                        message.content, list
                    ):
                        for content_part in message.content:
                            if (
                                isinstance(content_part, dict)
                                and content_part.get("type") == "image_url"
                            ):
                                return True
            elif hasattr(prompt, "content") and isinstance(prompt.content, list):
                for content_part in prompt.content:
                    if (
                        isinstance(content_part, dict)
                        and content_part.get("type") == "image_url"
                    ):
                        return True
            return False
        except Exception:
            return False

    @classmethod
    def get_user_usage_summary(cls, user_id: str, days: int = 30) -> Dict[str, Any]:
        """Get usage summary for a user over the specified number of days"""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                set_rls_context(cursor, conn, user_id, log_prefix="[LLMUsage]")

                # Get LLM summary statistics (exclude non-LLM cost rows)
                cursor.execute(
                    """
                    SELECT 
                        COUNT(*) as total_requests,
                        SUM(input_tokens) as total_input_tokens,
                        SUM(output_tokens) as total_output_tokens,
                        SUM(total_tokens) as total_tokens,
                        SUM(estimated_cost) as total_cost,
                        AVG(response_time_ms) as avg_response_time,
                        COUNT(DISTINCT model_name) as unique_models,
                        COUNT(DISTINCT session_id) as unique_sessions
                    FROM llm_usage_tracking
                    WHERE user_id = %s 
                    AND timestamp >= NOW() - INTERVAL '%s days'
                    AND request_type NOT LIKE '%%\\_cost' ESCAPE '\\'
                """,
                    (user_id, days),
                )

                summary = cursor.fetchone()

                # Get LLM usage by model (exclude non-LLM cost rows)
                cursor.execute(
                    """
                    SELECT 
                        model_name,
                        COUNT(*) as requests,
                        SUM(total_tokens) as tokens,
                        SUM(estimated_cost) as cost
                    FROM llm_usage_tracking
                    WHERE user_id = %s 
                    AND timestamp >= NOW() - INTERVAL '%s days'
                    AND request_type NOT LIKE '%%\\_cost' ESCAPE '\\'
                    GROUP BY model_name
                    ORDER BY cost DESC
                """,
                    (user_id, days),
                )

                model_usage = cursor.fetchall()

                return {
                    "summary": {
                        "total_requests": summary[0] or 0,
                        "total_input_tokens": summary[1] or 0,
                        "total_output_tokens": summary[2] or 0,
                        "total_tokens": summary[3] or 0,
                        "total_cost": float(summary[4]) if summary[4] else 0.0,
                        "avg_response_time_ms": float(summary[5])
                        if summary[5]
                        else 0.0,
                        "unique_models": summary[6] or 0,
                        "unique_sessions": summary[7] or 0,
                    },
                    "by_model": [
                        {
                            "model_name": row[0],
                            "requests": row[1],
                            "tokens": row[2],
                            "cost": float(row[3]),
                        }
                        for row in model_usage
                    ],
                }

        except Exception as e:
            logger.error(f"Error getting usage summary: {e}")
            return {"summary": {}, "by_model": []}

    @classmethod
    def get_pricing_info(cls) -> Dict[str, Any]:
        """Get information about current pricing sources and cache status"""
        provider_mode = os.getenv("LLM_PROVIDER_MODE")

        if provider_mode != "openrouter":
            provider_cache = {}
            gemini_dynamic = False
            try:
                provider_svc = get_provider_pricing_service()
                provider_cache = provider_svc.get_cache_info()
                gemini_dynamic = provider_cache.get("models_cached", 0) > 0
            except Exception as e:
                logger.debug("Could not fetch provider pricing cache info: %s", e)

            return {
                "dynamic_pricing_enabled": gemini_dynamic,
                "pricing_source": "provider_billing_api+static"
                if gemini_dynamic
                else "static",
                "fallback_models_count": len(cls.MODEL_PRICING),
                "provider_mode": provider_mode,
                "provider_cache": provider_cache,
                "note": "Dynamic billing API pricing applies to Google/Gemini models only; "
                "Anthropic and OpenAI use static rates.",
            }

        try:
            pricing_service = get_pricing_service()
            cache_info = pricing_service.get_cache_info()

            return {
                "dynamic_pricing_enabled": True,
                "cache_info": cache_info,
                "fallback_models_count": len(cls.MODEL_PRICING),
                "pricing_source": "openrouter_api"
                if cache_info.get("has_api_key")
                else "static_fallback",
            }
        except Exception as e:
            # Log full exception details on the server, but do not expose them to clients
            logger.warning("Error getting pricing info", exc_info=True)
            return {
                "dynamic_pricing_enabled": False,
                # Return a generic error message that does not reveal internal details
                "error": "Failed to load dynamic pricing data",
                "fallback_models_count": len(cls.MODEL_PRICING),
                "pricing_source": "static_fallback",
            }

    @classmethod
    def refresh_pricing(cls) -> bool:
        """Manually refresh pricing from OpenRouter API"""
        try:
            pricing_service = get_pricing_service()
            return pricing_service.refresh_pricing()
        except Exception as e:
            logger.error(f"Error refreshing pricing: {e}")
            return False


def tracked_invoke(
    llm,
    messages,
    *,
    user_id: str,
    session_id: Optional[str] = None,
    model_name: str,
    request_type: str,
    api_provider: Optional[str] = None,
    provider_mode: Optional[str] = None,
    org_id: Optional[str] = None,
):
    """Invoke an LLM and automatically track the usage.

    Drop-in replacement for ``llm.invoke(messages)`` that records tokens
    and cost into ``llm_usage_tracking`` via ``LLMUsageTracker.track_llm_call``.
    Returns the raw LLM response so callers work exactly as before.

    ``api_provider`` defaults to the provider that actually serves ``model_name``
    (``resolve_serving_provider``), so a Claude call under
    ``LLM_PROVIDER_MODE=bedrock`` is billed to ``bedrock``; the same label is used
    as the pricing ``provider_mode`` unless the caller passes one, so Bedrock-served
    rows carry the regional premium whatever the global routing mode is.
    """
    start_time = time.time()
    error_message = None
    response = None
    resolved_provider = api_provider or resolve_serving_provider(model_name, provider_mode)
    try:
        response = llm.invoke(messages)
        return response
    except Exception as e:
        error_message = str(e)
        raise
    finally:
        try:
            LLMUsageTracker.track_llm_call(
                user_id=user_id,
                session_id=session_id,
                model_name=model_name,
                request_type=request_type,
                prompt=messages,
                response=response,
                start_time=start_time,
                error_message=error_message,
                api_provider=resolved_provider,
                provider_mode=provider_mode or resolved_provider,
                org_id=org_id,
            )
        except Exception as track_err:
            logger.warning(f"Failed to track LLM usage for {request_type}: {track_err}")


class UsageTrackingCallback(BaseCallbackHandler):
    """LangChain callback that records every model call of an agent run.

    Attached to a streaming model (``callbacks=[...]`` at construction) or passed
    through ``invoke(..., config={"callbacks": [cb]})`` for a non-streaming agent
    such as the memory extractor. Each ``on_llm_end`` stores one
    ``llm_usage_tracking`` row (through ``LLMUsageTracker.store_usage``, which sets
    the RLS context) using the provider-reported ``usage_metadata``.
    """

    def __init__(
        self,
        user_id: str,
        session_id: Optional[str],
        model_name: str,
        api_provider: str,
        request_type: str = "agent_workflow",
        request_metadata: Optional[Dict[str, Any]] = None,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.model_name = model_name
        self.api_provider = api_provider
        self.request_type = request_type
        self.request_metadata = request_metadata or {"agent_executor": True}
        self.current_calls: Dict[Any, Dict[str, float]] = {}
        self.calls_tracked = 0

    def on_llm_start(self, serialized, prompts, run_id=None, **kwargs):
        """Record call start time."""
        try:
            if run_id:
                self.current_calls[run_id] = {"start_time": time.time()}
        except Exception as e:
            logger.warning(f"Error tracking agent LLM start: {e}")

    def on_chat_model_start(self, serialized, messages, run_id=None, **kwargs):
        """Chat models emit this instead of on_llm_start."""
        self.on_llm_start(serialized, messages, run_id=run_id, **kwargs)

    def on_llm_end(self, response, run_id=None, **kwargs):
        """Track LLM call end using provider-reported usage_metadata."""
        try:
            if not run_id or run_id not in self.current_calls:
                return

            call_info = self.current_calls.pop(run_id)

            counts = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
            }

            # Extract real token counts from provider usage_metadata (whole prompt,
            # with the cache read / write breakdown)
            if hasattr(response, "generations"):
                for gen_list in response.generations:
                    for gen in gen_list:
                        msg = getattr(gen, "message", None)
                        if msg and getattr(msg, "usage_metadata", None):
                            counts = LLMUsageTracker.extract_token_counts(msg.usage_metadata)
                            break
                    if counts["input_tokens"] > 0:
                        break

            # Also check llm_output for OpenAI-style token_usage
            if counts["input_tokens"] == 0 and hasattr(response, "llm_output") and response.llm_output:
                token_usage = response.llm_output.get("token_usage", {})
                if isinstance(token_usage, dict) and token_usage:
                    counts = LLMUsageTracker.extract_token_counts(token_usage)

            input_tokens = counts["input_tokens"]
            output_tokens = counts["output_tokens"]
            cached_input_tokens = counts["cached_input_tokens"]
            cache_write_tokens = counts["cache_write_tokens"]

            if input_tokens == 0 and output_tokens == 0:
                logger.warning(f"No provider usage_metadata for {self.model_name} - tokens will be 0")

            # The serving provider doubles as the pricing mode: a Bedrock-served row
            # carries the regional premium even when LLM_PROVIDER_MODE is "direct"
            # with a pinned bedrock/ id.
            estimated_cost = LLMUsageTracker.calculate_cost(
                input_tokens, output_tokens, self.model_name,
                provider_mode=self.api_provider or None,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
            )

            usage = LLMUsage(
                user_id=self.user_id,
                session_id=self.session_id,
                model_name=self.model_name,
                api_provider=self.api_provider,
                request_type=self.request_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                response_time_ms=int((time.time() - call_info["start_time"]) * 1000),
                error_message=None,
                request_metadata=dict(self.request_metadata),
                cache_read_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_tokens,
            )

            if LLMUsageTracker.store_usage(usage):
                self.calls_tracked += 1
                logger.info(
                    f"Agent LLM Tracked ({self.request_type}): {self.model_name} - "
                    f"{input_tokens}+{output_tokens} tokens "
                    f"(cache read {cached_input_tokens}, write {cache_write_tokens}) - ${estimated_cost:.6f}"
                )
            else:
                logger.warning("Failed to store agent LLM usage")
        except Exception as e:
            logger.warning(f"Error tracking agent LLM end: {e}")
