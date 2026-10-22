"""Provider-neutral per-call options and Claude model-family facts.

Aurora builds chat models through ``create_chat_model(model, **kwargs)`` and the
kwargs land on whichever LangChain client the provider picks. The clients do not
agree on names: ``ChatOpenAI`` wants ``request_timeout``, ``ChatAnthropic`` and
``ChatBedrockConverse`` want ``timeout``, Gemini wants ``max_output_tokens``,
Ollama wants ``num_predict`` — and ``ChatBedrockConverse`` moves any unknown
kwarg into the request body, where Bedrock rejects it with a
``ValidationException``. Callers therefore express the few options they need in
ONE spelling, and each provider's ``get_chat_model`` translates them through
:func:`pop_call_options`:

- ``max_tokens``       — output cap for the call.
- ``request_timeout``  — seconds; the provider maps it to its own timeout field.
- ``disable_thinking`` — turn adaptive/extended thinking off for classifier and
  summary calls. Only sent to models AWS/Anthropic document as accepting
  ``{"thinking": {"type": "disabled"}}`` (see :func:`supports_thinking_disable`);
  everything else silently drops it so the request can never 400.

``max_tokens`` and ``disable_thinking`` are coupled: an auxiliary caller sizes
its cap for an answer with no chain-of-thought in it (a 400-token verdict, a
32-token severity word). When the provider cannot actually switch thinking off
for the model it is building — the field is not portable (Bedrock gateway,
OpenRouter), the model rejects it (Opus 5.5), or it is another vendor's
reasoning model (GPT-5.x, Gemini 2.5+) — reasoning tokens would eat that budget
and the answer would come back truncated or empty, so the provider drops the
cap and the client keeps its own default (the pre-cap behaviour). See
:func:`effective_max_tokens`.

This module is deliberately import-light (stdlib only) and lives outside the
``providers`` package so the provider modules — which tests load by source with
their relative imports stubbed — can import it with an absolute import.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

# The cheap tier every auxiliary call (judge, summaries, memory, severity, ...)
# defaults to. Claude Haiku 5.5: 1M context, adaptive thinking that can be
# disabled, $0.10/$0.50 per MTok for prompts <= 100K tokens (Bedrock model card
# + Anthropic pricing page, read 2026-10-22).
CHEAP_MODEL = "anthropic/claude-haiku-5.5"
# The same model as a Bedrock inference-profile id, minus the geo prefix: the 5.x
# generation carries no date suffix (model card: ``eu.anthropic.claude-haiku-5-5``).
CHEAP_MODEL_BEDROCK_BASE = "anthropic.claude-haiku-5-5"
# Geo prefixes under which AWS publishes a Haiku 5.5 inference profile (model card,
# read 2026-10-22): us., eu., au., jp. and global. — there is NO ``apac.`` profile for
# the Claude 5.x generation, so an id derived for an ap-* region would 404.
CHEAP_MODEL_BEDROCK_GEOS = ("us", "eu", "au", "jp", "global")

# Provider modes that serve an ``anthropic/`` id themselves, without ANTHROPIC_API_KEY:
# OpenRouter routes every model; forced Anthropic is the Claude vendor. Bedrock is the
# third one, but only once it has a gateway URL or a region (see cheap_tier_routable).
_CLAUDE_SERVING_MODES = ("openrouter", "anthropic")
# Region vars BedrockProvider reads, in precedence order (BEDROCK_* first, then AWS_*).
_BEDROCK_REGION_VARS = ("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
# `BEDROCK_REGION=global` selects Bedrock's global inference profiles (a profile geo,
# not an AWS region — BedrockProvider still signs against a real region).
BEDROCK_GLOBAL_GEO = "global"


def bedrock_geo_for_region(region: Optional[str]) -> str:
    """Bedrock cross-region inference-profile geo prefix derived from the AWS region.

    ``eu-*`` -> ``eu``, ``ap-*`` -> ``apac``, ``global`` -> ``global``, anything else
    (including unset) -> ``us``. Single source of truth shared with
    ``providers.bedrock_provider`` so routability and id derivation cannot drift apart.
    """
    r = (region or "us-east-1").lower()
    if r == BEDROCK_GLOBAL_GEO:
        return BEDROCK_GLOBAL_GEO
    if r.startswith("eu"):
        return "eu"
    if r.startswith("ap"):
        return "apac"
    return "us"


def _bedrock_region(env: Mapping[str, str]) -> Optional[str]:
    """The region BedrockProvider would resolve from ``env`` (None when unset)."""
    for var in _BEDROCK_REGION_VARS:
        if env.get(var):
            return env[var]
    return None


def cheap_tier_routable(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when ``CHEAP_MODEL`` can be served under the configured provider settings.

    Mirrors ``ProviderRegistry.get_provider_for_model`` on environment variables only —
    no provider is probed (Ollama's availability check is an HTTP call), so this is safe
    to evaluate at import time:

    - ``LLM_PROVIDER_MODE=openrouter`` / ``anthropic``: that provider serves every Claude
      id. If its key is missing, ``MAIN_MODEL`` fails the same way, so nothing is lost.
    - ``LLM_PROVIDER_MODE=bedrock`` with a gateway URL: the gateway receives the same
      derived profile names as ``MAIN_MODEL``, so the cheap tier is as routable as MAIN.
    - ``LLM_PROVIDER_MODE=bedrock`` with a region (native mode): Bedrock serves
      ``anthropic/`` ids translated to the region's inference-profile id — but only when
      AWS publishes a Haiku 5.5 profile for that geo (``CHEAP_MODEL_BEDROCK_GEOS``). An
      ``ap-*`` region derives ``apac.``, which does not exist for the 5.x generation, so
      the cheap tier is NOT routable there and the variables follow ``MAIN_MODEL``.
    - anything else — ``direct`` / ``auto`` (or unset), or a forced ``vertex`` /
      ``google`` / ``openai`` / ``ollama`` provider that cannot serve Claude and falls
      back to prefix routing — reaches Claude only through the Anthropic provider, which
      needs ``ANTHROPIC_API_KEY``.
    """
    env = os.environ if env is None else env
    mode = (env.get("LLM_PROVIDER_MODE") or "direct").strip().lower()
    if mode in _CLAUDE_SERVING_MODES:
        return True
    if mode == "bedrock":
        if env.get("BEDROCK_BASE_URL"):
            return True
        region = _bedrock_region(env)
        if region:
            return bedrock_geo_for_region(region) in CHEAP_MODEL_BEDROCK_GEOS
        # Forced bedrock with nothing configured: the provider raises for every model;
        # fall through so the outcome matches direct routing.
    return bool(env.get("ANTHROPIC_API_KEY"))


def bedrock_pinned_cheap_model(main_model: str, env: Mapping[str, str]) -> Optional[str]:
    """Cheap tier for a ``MAIN_MODEL`` pinned to an explicit ``bedrock/<id>``.

    Returns ``None`` when ``main_model`` is not a ``bedrock/`` id. Otherwise the result
    stays on the operator's Bedrock route in every ``LLM_PROVIDER_MODE`` — never another
    provider (data residency) and never an id the pinned geo does not publish (the
    fail-closed judge and input rail would block every request):

    - native mode (no ``BEDROCK_BASE_URL``) and the pin is a Claude inference profile
      on a geo that publishes Haiku 5.5 (``us`` / ``eu`` / ``au`` / ``jp`` / ``global``):
      ``bedrock/<geo>.anthropic.claude-haiku-5-5`` — same geo, same account, same
      Anthropic model access as the pinned model.
    - anything else — gateway mode (the gateway's names are its own), an ``apac.`` or
      unknown prefix, an ARN, a non-Anthropic model (Nova / Llama / Mistral accounts
      may lack Anthropic model access): ``main_model`` itself.
    """
    raw = (main_model or "").strip()
    prefix, sep, native = raw.partition("/")
    if not sep or prefix.lower() != "bedrock" or not native:
        return None
    if env.get("BEDROCK_BASE_URL"):
        return raw
    geo, dot, rest = native.partition(".")
    if (
        dot
        and geo.lower() in CHEAP_MODEL_BEDROCK_GEOS
        and rest.lower().startswith("anthropic.claude")
    ):
        return f"bedrock/{geo.lower()}.{CHEAP_MODEL_BEDROCK_BASE}"
    return raw


def default_auxiliary_model(main_model: str, env: Optional[Mapping[str, str]] = None) -> str:
    """The model every background / classifier call defaults to.

    A ``MAIN_MODEL`` pinned to an explicit ``bedrock/<id>`` is handled first, in every
    mode (see :func:`bedrock_pinned_cheap_model`): the cheap tier stays on that Bedrock
    geo or follows ``main_model``. Otherwise ``CHEAP_MODEL`` when
    :func:`cheap_tier_routable`, else ``main_model`` — the model the deployment serves by
    definition. Without these fallbacks an OpenAI / Google / Vertex / Ollama-only
    install, a Bedrock gateway pinned to explicit ``bedrock/`` ids, or an APAC Bedrock
    install would hand the fail-closed judge and input rail a model nobody can route,
    and every chat message and command would be blocked after an upgrade; a pinned EU
    Bedrock install with a stray ``ANTHROPIC_API_KEY`` would silently send background
    traffic to the Anthropic API instead.
    """
    env = os.environ if env is None else env
    pinned = bedrock_pinned_cheap_model(main_model, env)
    if pinned is not None:
        return pinned
    return CHEAP_MODEL if cheap_tier_routable(env) else main_model

# Bedrock inference-profile geo prefixes (``us.anthropic...``) and the vendor
# prefix, stripped when normalizing an id to its Claude family name.
_GEO_PREFIXES = ("us.", "eu.", "apac.", "au.", "jp.", "global.")
_VENDOR_PREFIXES = ("anthropic.", "anthropic/")
# Dated / versioned Bedrock suffixes: ``-20251001-v1:0``, ``-v1:0``, ``-v1``.
_VERSION_SUFFIX = re.compile(r"(-\d{8})?-v\d+(:\d+)?$")

# Claude families where ``{"thinking": {"type": "disabled"}}`` is accepted.
# Sources (read 2026-10-22): Bedrock model cards for Haiku 5.5 ("adaptive thinking
# is on by default and can be turned off"), Sonnet 5, Opus 5, Opus 4.7/4.8, and
# the Bedrock adaptive-thinking page. Prefix match on the normalized family name.
THINKING_DISABLE_SUPPORTED = (
    "claude-haiku-5-5",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
)
# Checked BEFORE the allowlist: ``claude-sonnet-5`` is a prefix of
# ``claude-sonnet-5-5`` and ``claude-opus-5`` of ``claude-opus-5-5``, but Opus 5.5
# runs thinking always-on (cannot be disabled), Sonnet 5.5 rejects ``disabled``,
# and Fable/Mythos return 400 on any explicit thinking field.
THINKING_DISABLE_UNSUPPORTED = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable",
    "claude-mythos",
)

# Claude families that run adaptive thinking when NO ``thinking`` parameter is sent
# at all — the 5.x generation. Source: langchain-aws 1.9.0 ``thinking_on_by_default``
# (``claude-sonnet-5``, ``claude-opus-5``, ``claude-fable-5``, ``claude-haiku-5``) and
# the Haiku 5.5 model card ("adaptive thinking is on by default"). Mythos rejects every
# explicit thinking field and its default is not documented, so it is assumed on
# (fail-safe). Everything older (4.x, including Opus 4.7/4.8) thinks only when a
# ``thinking`` block is sent, which Aurora never does for auxiliary calls.
THINKING_ON_BY_DEFAULT = (
    "claude-haiku-5",
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-fable",
    "claude-mythos",
)

# Gemini model-name fragments that mark a thinking model (2.5+, 3, 4). These models
# think by default and Aurora never turns that off (``GEMINI_DISABLE_THINKING`` only
# stops the thoughts from being returned). Shared with
# ``providers.base_provider.apply_gemini_thinking_config`` so the two lists cannot
# drift. ``flash-lite`` variants ship with thinking off.
GEMINI_THINKING_INDICATORS = (
    "gemini-4", "gemini-3", "gemini-2.5",
    "2.5-pro", "2.5-flash",
    "3-pro", "3-flash",
)
# Gemini ids that accept the ``thinking_level`` parameter (preview / experimental
# only; GA models such as gemini-2.5-pro think by default but reject it).
GEMINI_THINKING_LEVEL_INDICATORS = ("preview", "exp")

# OpenAI model families with no reasoning at all. Everything else OpenAI ships
# (gpt-5+, o-series) reasons by default — and ``OpenAIProvider`` forces
# ``reasoning.effort=high`` on exactly that set.
_OPENAI_NON_REASONING_PREFIXES = ("gpt-3", "gpt-4")
_OPENAI_NAME = re.compile(r"^(gpt-|o\d)")

# Smallest cacheable prefix per family, in tokens — "Minimum number of tokens per
# cache checkpoint" from the Bedrock prompt-caching page (table re-read 2026-10-22;
# the Anthropic API publishes the same per-model minimums). The minimum applies to
# the whole prefix before a checkpoint (tools + system + messages); a checkpoint on a
# shorter prefix is accepted but nothing is cached. Prefix match on the normalized
# family name, first hit wins, so ``claude-sonnet-5-5`` must precede ``claude-sonnet-5``.
# Unknown families fall back to the largest published minimum so a cache WRITE is
# never spent on a prefix too small to cache.
_MIN_CACHEABLE_TOKENS = (
    ("claude-haiku-5-5", 512),
    ("claude-opus-5-5", 512),
    ("claude-sonnet-5-5", 512),
    ("claude-fable-5", 512),  # Fable 5 and 5.1
    ("claude-mythos-5", 512),  # Mythos 5 and 5.1
    ("claude-opus-5", 512),
    ("claude-sonnet-5", 1024),
    ("claude-opus-4-8", 1024),
    ("claude-sonnet-4-6", 1024),
    ("claude-sonnet-4-5", 1024),
    ("claude-haiku-4-5", 4096),
    ("claude-opus-4-5", 4096),
    ("claude-opus-4-6", 4096),
    ("claude-opus-4-7", 4096),
)
_DEFAULT_MIN_CACHEABLE_TOKENS = 4096


def claude_family(model_id: str) -> str:
    """Normalize any spelling of a Claude id to its dashed family name.

    ``bedrock/eu.anthropic.claude-haiku-5-5`` -> ``claude-haiku-5-5``
    ``anthropic/claude-opus-5.5``              -> ``claude-opus-5-5``
    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` -> ``claude-haiku-4-5``

    Non-Claude ids come back lower-cased with the same prefixes stripped, so
    callers can test ``startswith("claude")`` on the result.
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
    name = _VERSION_SUFFIX.sub("", name)
    return name.replace(".", "-")


# ``claude-haiku-5-5`` -> ``claude-haiku-5.5``: Aurora's catalog tables (pricing, context
# limits, cutoffs, picker) key Claude models by their dotted marketing version while
# Bedrock and the Anthropic API spell the same version with a dash. Only a
# ``-<digit>-<digit>`` version pair is dotted, so ``claude-3-haiku`` is left alone.
_CLAUDE_VERSION_DASHES = re.compile(r"-(\d)-(\d)(?=$|-)")


def claude_catalog_ids(model_id: str) -> Tuple[str, ...]:
    """Aurora catalog keys a Claude id may be filed under, most specific first.

    ``bedrock/eu.anthropic.claude-haiku-5-5`` -> ``("anthropic/claude-haiku-5-5",
    "anthropic/claude-haiku-5.5")``; ``anthropic.claude-haiku-4-5-20251001-v1:0`` ->
    ``("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4.5")``. Empty for a
    non-Claude id, so callers can fall through to their own heuristics.
    """
    family = claude_family(model_id)
    if not family.startswith("claude"):
        return ()
    dotted = _CLAUDE_VERSION_DASHES.sub(r"-\1.\2", family)
    ids = [f"anthropic/{family}"]
    if dotted != family:
        ids.append(f"anthropic/{dotted}")
    return tuple(ids)


def supports_thinking_disable(model_id: str) -> bool:
    """True when the model documents ``thinking: {type: disabled}`` as accepted."""
    family = claude_family(model_id)
    if not family.startswith("claude"):
        return False
    if any(family.startswith(blocked) for blocked in THINKING_DISABLE_UNSUPPORTED):
        return False
    return any(family.startswith(allowed) for allowed in THINKING_DISABLE_SUPPORTED)


def min_cacheable_tokens(model_id: str) -> int:
    """Minimum prompt-prefix length (tokens) Bedrock/Anthropic will cache for the model."""
    family = claude_family(model_id)
    for prefix, minimum in _MIN_CACHEABLE_TOKENS:
        if family.startswith(prefix):
            return minimum
    return _DEFAULT_MIN_CACHEABLE_TOKENS


def gemini_thinks_by_default(model_name: str) -> bool:
    """True for Gemini ids that think unless told otherwise (2.5+ / 3 / 4, not flash-lite)."""
    name = (model_name or "").lower()
    if "flash-lite" in name:
        return False
    return any(indicator in name for indicator in GEMINI_THINKING_INDICATORS)


def reasons_by_default(model_id: str) -> bool:
    """Best-effort: does ``model_id`` spend output tokens on reasoning / thinking when
    the request carries no thinking parameter?

    Decided per vendor from the model name alone (any spelling: ``anthropic/``,
    ``bedrock/<geo>.anthropic.``, OpenRouter ``vendor/model``, bare native ids):

    - Claude: the 5.x generation (``THINKING_ON_BY_DEFAULT``) does, 4.x does not.
    - OpenAI: gpt-3 / gpt-4 families do not; gpt-5+ and the o-series do.
    - Gemini: 2.5+ / 3 / 4 do (``flash-lite`` and older ones do not).
    - Anything else — Nova, Llama, Mistral, DeepSeek, Qwen, gpt-oss, local Ollama
      models, unknown ids — is assumed to reason. That is the fail-safe direction:
      the only consumer drops an output cap on True, which can never truncate an
      answer; a wrong False would keep a cap sized for a thinking-free reply on a
      model that thinks.
    """
    name = (model_id or "").strip().lower()
    family = claude_family(name)
    if family.startswith("claude"):
        return any(family.startswith(prefix) for prefix in THINKING_ON_BY_DEFAULT)
    if "gemini" in name:
        return gemini_thinks_by_default(name)
    bare = name[len("openai/"):] if name.startswith("openai/") else name
    if _OPENAI_NAME.match(bare):
        return not bare.startswith(_OPENAI_NON_REASONING_PREFIXES)
    return True


# ---------------------------------------------------------------------------
# Output-token budgets per call type
# ---------------------------------------------------------------------------

CallType = Literal["judge", "severity", "summary", "report", "agent", "memory_agent"]

# Default ``max_tokens`` per call type. Without a cap a classifier that goes off
# the rails, or an agent turn that keeps generating, is billed to the model's
# own maximum output (128K on the Claude 5.x generation). The budgets are
# deliberately generous for the shape of each answer:
# - judge:        a three-field JSON verdict (observation / thought / conclusion)
# - severity:     one word
# - summary:      tool-output / alert / synopsis / metadata summaries (~500 words)
# - report:       RCA summaries, hypotheses, diagnostics plans, recommendations
# - agent:        one streaming agent turn; thinking tokens count against it
# - memory_agent: one turn of the memory-extraction agent (tool calls + notes)
_DEFAULT_MAX_TOKENS: Dict[str, int] = {
    "judge": 400,
    "severity": 32,
    "summary": 1500,
    "report": 4000,
    "agent": 16000,
    "memory_agent": 2000,
}


def _max_tokens_env(call_type: str) -> Optional[str]:
    """Raw env override for a call type. Each variable is spelled out literally so
    ``scripts/validate-env-vars.sh`` (which greps ``os.getenv("...")``) sees them."""
    if call_type == "judge":
        return os.getenv("LLM_MAX_TOKENS_JUDGE")
    if call_type == "severity":
        return os.getenv("LLM_MAX_TOKENS_SEVERITY")
    if call_type == "summary":
        return os.getenv("LLM_MAX_TOKENS_SUMMARY")
    if call_type == "report":
        return os.getenv("LLM_MAX_TOKENS_REPORT")
    if call_type == "agent":
        return os.getenv("LLM_MAX_TOKENS_AGENT")
    if call_type == "memory_agent":
        return os.getenv("LLM_MAX_TOKENS_MEMORY_AGENT")
    return None


def max_tokens_for(call_type: CallType) -> int:
    """Output-token cap for a call type, honouring ``LLM_MAX_TOKENS_<TYPE>``.

    Unset, empty (Docker Compose forwards ``${VAR}`` as "") and non-numeric or
    non-positive values all mean "use the default", so a misconfigured variable
    can never produce a zero-token or negative cap.
    """
    if call_type not in _DEFAULT_MAX_TOKENS:
        raise ValueError(f"Unknown call type: {call_type!r}")
    default = _DEFAULT_MAX_TOKENS[call_type]
    raw = (_max_tokens_env(call_type) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class CallOptions:
    """The provider-neutral options a caller may pass to ``create_chat_model``."""

    max_tokens: Optional[int] = None
    request_timeout: Optional[float] = None
    disable_thinking: bool = False


def pop_call_options(kwargs: Dict[str, Any]) -> CallOptions:
    """Remove the neutral options from ``kwargs`` (in place) and return them.

    Everything else stays in ``kwargs`` and is forwarded to the client untouched,
    so provider-specific extras (``callbacks``, ``streaming``, ...) keep working.
    """
    max_tokens = kwargs.pop("max_tokens", None)
    request_timeout = kwargs.pop("request_timeout", None)
    disable_thinking = bool(kwargs.pop("disable_thinking", False))
    return CallOptions(
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        request_timeout=float(request_timeout) if request_timeout is not None else None,
        disable_thinking=disable_thinking,
    )


def effective_max_tokens(
    opts: CallOptions, model_id: str, *, thinking_disabled: bool = False
) -> Optional[int]:
    """The output cap a provider should actually send for ``opts``.

    ``thinking_disabled`` says whether the provider is sending an explicit
    "thinking off" field for this model (Bedrock native / Anthropic direct on a
    ``supports_thinking_disable`` family). The cap passes through unless all three
    hold: the caller asked for ``disable_thinking``, the provider is NOT switching
    it off, and the model :func:`reasons_by_default` — then reasoning tokens would
    be charged against a budget sized for a thinking-free answer (the judge's 400,
    severity's 32, a summary's 1500) and the reply would come back cut off or
    empty, so ``None`` is returned and the client keeps its default output limit,
    exactly as before the caps existed. A cap set WITHOUT ``disable_thinking``
    (the agent's per-turn budget, where thinking tokens are meant to count) is the
    caller's explicit decision and is never touched.
    """
    if opts.max_tokens is None:
        return None
    if opts.disable_thinking and not thinking_disabled and reasons_by_default(model_id):
        return None
    return opts.max_tokens
