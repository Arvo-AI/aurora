"""
AWS Bedrock provider implementation.

One provider, two modes, auto-selected from configuration:

1. Gateway mode (OpenAI-compatible) — when ``BEDROCK_BASE_URL`` is set, Aurora
   sends OpenAI-shaped requests (``POST .../v1/chat/completions``) to that URL via
   ``ChatOpenAI``. This is the customer's case: a Bedrock endpoint inside their VPC
   (ALB + Lambda) that exposes an OpenAI-compatible API, typically with no API key
   (the VPC boundary handles auth). Reuses the same plumbing as OpenRouter.

2. Native mode (AWS SDK) — when no base URL is set, Aurora talks to AWS Bedrock
   directly via ``langchain_aws.ChatBedrockConverse`` using region + AWS
   credentials (or an IAM role via boto3's default chain). On-demand Claude on
   Bedrock requires an inference-profile id (e.g. ``us.anthropic.claude-...``),
   not the bare model id.

Set ``BEDROCK_REGION=global`` to route through Bedrock's ``global.`` inference
profiles, which bill at the direct list price instead of the ~10% cross-region
premium. ``global`` names a profile geo rather than an AWS endpoint, so boto3 is
still signed against a real region (``AWS_REGION`` / ``AWS_DEFAULT_REGION``,
defaulting to us-east-1).

Configuration is environment-variable based, like Ollama and Vertex. Bedrock-
specific ``BEDROCK_*`` vars take precedence over the standard ``AWS_*`` vars.
Native-mode clients carry a botocore ``Config`` with ``standard`` retries
(``BEDROCK_MAX_ATTEMPTS``, default 3 attempts), a 10 s connect timeout and a
``BEDROCK_READ_TIMEOUT`` (default 300 s) read timeout sized for streaming agent
turns; small classifier calls pass ``request_timeout`` to shrink it per call.
"""

import logging
import math
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from chat.backend.agent.bedrock_env import (
    GLOBAL_HOME_REGION_FALLBACK,
    resolve_bedrock_region,
    resolve_client_region,
)
from chat.backend.agent.call_options import (
    BEDROCK_GLOBAL_GEO,
    bedrock_geo_for_region,
    effective_max_tokens,
    pop_call_options,
    supports_thinking_disable,
)

from .base_provider import BaseLLMProvider
from ._sampling_guard import make_adaptive_sampling_cls
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)

# Anthropic native model name (dashed) -> Bedrock inference-profile base (without the
# region/geo prefix). Only models whose Bedrock id carries a version/date suffix that
# can't be derived from the name are listed here; bare models (e.g. claude-opus-5-5,
# claude-sonnet-5, claude-haiku-5-5 — the 5.x generation has no date suffix) derive
# `anthropic.<name>` automatically. The geo prefix (us./eu./apac.) is prepended at
# runtime from the configured region, so this stays region-agnostic.
_ANTHROPIC_TO_BEDROCK_BASE = {
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Models AWS publishes only as `us.` and `global.` inference profiles — no `eu.` or
# `apac.` variant exists. Deriving one from the region would yield a 404 on an EU/APAC
# install, so those geos route to the `global.` profile, which is listed in every region.
_GLOBAL_ONLY_GEO_MODELS = {
    "claude-fable-5-1",
    "claude-fable-5",
}

# `BEDROCK_REGION=global` selects Bedrock's global inference profiles (billed at the
# direct rate, no cross-region premium). It names a profile geo, not an AWS endpoint.
_GLOBAL_GEO = BEDROCK_GLOBAL_GEO
# boto3 still has to sign against a real region when the global profile is in use
# (shared with the embedding client through chat.backend.agent.bedrock_env).
_GLOBAL_HOME_REGION_FALLBACK = GLOBAL_HOME_REGION_FALLBACK

# botocore client defaults for native mode. `standard` retry mode: exponential
# backoff with jitter on throttling / 5xx / transient connection errors, capped by
# `total_max_attempts` (the first try + retries) — `adaptive` is still marked
# experimental and its rate limiter is per client instance. The read timeout has
# to cover a long streaming agent turn (Opus 5.5 with thinking on can hold the
# stream for minutes), so it defaults to 300 s; a per-call `request_timeout`
# shrinks it for the small classifier calls.
_DEFAULT_CONNECT_TIMEOUT = 10
_DEFAULT_READ_TIMEOUT = 300
_DEFAULT_MAX_ATTEMPTS = 3
_RETRY_MODE = "standard"
# Gateway mode (ChatOpenAI): retries are handled by the openai client.
_GATEWAY_MAX_RETRIES = 2


def _positive_int_env(name_value: str | None, default: int) -> int:
    """``int(value)`` with "unset / empty / junk / <= 0 means default" semantics."""
    raw = (name_value or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _botocore_retry_config(read_timeout: int | None = None, max_attempts: int | None = None):
    """botocore ``Config`` for the Bedrock runtime client.

    ``BEDROCK_READ_TIMEOUT`` (default 300 s) and ``BEDROCK_MAX_ATTEMPTS`` (default 3)
    are the deployment-wide defaults; a per-call ``request_timeout`` (small
    classifier calls) is passed in as ``read_timeout`` and also bounds the connect
    timeout. Imported lazily so the module can be loaded without botocore.
    """
    from botocore.config import Config

    effective_read = (
        read_timeout
        if read_timeout is not None
        else _positive_int_env(os.getenv("BEDROCK_READ_TIMEOUT"), _DEFAULT_READ_TIMEOUT)
    )
    effective_attempts = (
        max_attempts
        if max_attempts is not None
        else _positive_int_env(os.getenv("BEDROCK_MAX_ATTEMPTS"), _DEFAULT_MAX_ATTEMPTS)
    )
    return Config(
        retries={"mode": _RETRY_MODE, "total_max_attempts": effective_attempts},
        connect_timeout=min(_DEFAULT_CONNECT_TIMEOUT, effective_read),
        read_timeout=effective_read,
    )


def _geo_prefix_for(region: str) -> str:
    """Bedrock cross-region inference-profile geo prefix derived from the AWS region.

    Delegates to ``call_options.bedrock_geo_for_region`` — the same derivation decides,
    at config time, whether the cheap tier is routable in this region — so the two can
    never disagree. Opt-in ``global`` routing bills at the direct list price.
    """
    return bedrock_geo_for_region(region)


def _resolve_client_region(region: str | None) -> str | None:
    """Real AWS region for boto3, given a possibly-``global`` profile geo.

    ``global`` is not an AWS region: passing it to boto3 produces an unresolvable
    endpoint. Fall back to the standard AWS region vars, then to us-east-1, so
    ``BEDROCK_REGION=global`` is actually a configuration the provider supports —
    and so pricing (which treats ``global`` as premium-free) matches routing.
    Thin alias of ``bedrock_env.resolve_client_region`` so the chat provider and
    the embedding client can never disagree on it.
    """
    return resolve_client_region(region)


def _adaptive_bedrock_converse():
    """Lazily import ``ChatBedrockConverse`` (so gateway-only deployments don't need
    langchain-aws) and wrap it with the shared sampling self-heal. ``wrap_async=False``:
    Converse's async path dispatches to the sync one via ``BaseChatModel``, so wrapping
    async too would retry twice. ``ImportError`` propagates to the caller (kept as a
    friendly RuntimeError there)."""
    from langchain_aws import ChatBedrockConverse

    return make_adaptive_sampling_cls(ChatBedrockConverse, wrap_async=False)


def _translate_call_options(native_model: str, kwargs: dict, *, native_mode: bool) -> dict:
    """Map Aurora's provider-neutral call options onto this provider's client fields.

    Pops ``max_tokens`` / ``request_timeout`` / ``disable_thinking`` out of ``kwargs``
    (see ``chat.backend.agent.call_options``) and returns the constructor fields to
    merge into the client config:

    - native (``ChatBedrockConverse``): ``max_tokens``; ``request_timeout`` ->
      ``_read_timeout`` (integer seconds, consumed by ``get_chat_model`` and folded
      into the botocore ``Config`` as the client's read/connect timeout). It is NOT
      mapped to the class's own ``timeout`` field: langchain-aws merges that field
      over ``config`` as ``Config(connect_timeout=t, read_timeout=t, retries=None)``,
      and botocore's ``Config.merge`` copies the explicit ``None``, which would
      silently switch the retry policy off for every call that sets a timeout. An
      unknown kwarg such as ``request_timeout`` would be moved into the request body
      and rejected by Bedrock with a ValidationException, so it never passes through.
      ``disable_thinking`` -> ``additional_model_request_fields={"thinking":
      {"type": "disabled"}}``, but only for models that document disabling as
      supported (Opus 5.5 is always-on, Sonnet 5.5 rejects it, Fable 400s) —
      anything else drops the flag so the call cannot fail.
    - gateway (OpenAI-compatible ``ChatOpenAI``): ``max_tokens`` and ``request_timeout``
      as-is; ``disable_thinking`` is dropped (no portable spelling through a gateway).

    In both modes ``max_tokens`` only goes through when thinking is really off for
    the call (``call_options.effective_max_tokens``): a cap sized for a thinking-free
    verdict or summary is dropped when the flag could not be honoured — Opus 5.5 /
    Sonnet 5.5 / Fable natively, every Claude 5.x model through the gateway, and
    non-Claude reasoning models — so reasoning tokens can never truncate the answer.
    The cap is kept where it is safe (Haiku 5.5 natively with thinking disabled, the
    Claude 4.x generation, which thinks only when asked).
    """
    opts = pop_call_options(kwargs)
    translated: dict = {}
    thinking_disabled = (
        opts.disable_thinking and native_mode and supports_thinking_disable(native_model)
    )
    max_tokens = effective_max_tokens(opts, native_model, thinking_disabled=thinking_disabled)
    if max_tokens is not None:
        translated["max_tokens"] = max_tokens
    elif opts.max_tokens is not None:
        logger.info(
            "Dropping max_tokens=%s for %s: thinking cannot be disabled on this route, "
            "so a cap sized for a thinking-free answer could truncate it.",
            opts.max_tokens, native_model,
        )
    if opts.request_timeout is not None:
        if native_mode:
            translated["_read_timeout"] = int(math.ceil(opts.request_timeout))
        else:
            translated["request_timeout"] = opts.request_timeout
    if thinking_disabled:
        extra = dict(kwargs.pop("additional_model_request_fields", None) or {})
        extra["thinking"] = {"type": "disabled"}
        translated["additional_model_request_fields"] = extra
    return translated


class BedrockProvider(BaseLLMProvider):
    """AWS Bedrock provider with OpenAI-compatible gateway and native SDK modes."""

    def __init__(self):
        super().__init__()
        # Gateway mode (OpenAI-compatible URL fronting Bedrock). Presence of this
        # URL auto-selects gateway mode.
        self.base_url = os.getenv("BEDROCK_BASE_URL")
        # Gateway auth is typically unnecessary (VPC boundary handles it); ChatOpenAI
        # still requires a non-empty key, so fall back to a placeholder.
        self.api_key = os.getenv("BEDROCK_API_KEY") or "not-needed"

        # Native mode (AWS SDK). BEDROCK_* takes precedence over standard AWS_*
        # (same resolution the embedding client uses: bedrock_env.resolve_bedrock_region).
        self.region = resolve_bedrock_region()
        self.access_key = os.getenv("BEDROCK_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_key = os.getenv("BEDROCK_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
        self.session_token = os.getenv("BEDROCK_SESSION_TOKEN") or os.getenv("AWS_SESSION_TOKEN")
        self.profile = os.getenv("BEDROCK_PROFILE")

    @property
    def is_native_mode(self) -> bool:
        """True when requests go to AWS Bedrock through the SDK (no gateway URL)."""
        return not self.base_url

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        if not self.is_available():
            raise RuntimeError(
                "Bedrock provider is not available. Set BEDROCK_BASE_URL (gateway mode) "
                "or BEDROCK_REGION/AWS_REGION (native mode)."
            )

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Bedrock provider")

        native_model = self.get_native_model_name(model)
        # Neutral options (max_tokens / request_timeout / disable_thinking) become
        # client-specific constructor fields; anything else in kwargs passes through.
        call_fields = _translate_call_options(
            native_model, kwargs, native_mode=self.is_native_mode
        )

        # Gateway mode is auto-selected by the presence of BEDROCK_BASE_URL; otherwise
        # native mode (AWS SDK). is_available() above guarantees one of the two is set.
        if self.base_url:
            logger.info(f"Creating Bedrock (gateway) chat model: {native_model} (base_url={self.base_url})")
            config = {
                "model": native_model,
                "temperature": temperature,
                "openai_api_base": self.base_url,
                "openai_api_key": self.api_key,
                "request_timeout": 120.0,
                "max_retries": _GATEWAY_MAX_RETRIES,
                "stream_usage": True,
            }
            config.update(call_fields)
            config.update(kwargs)
            # Self-heal sampling params in case the gateway forwards temperature/top_p to a
            # model that rejects them (e.g. Opus 4.7+) instead of dropping them itself.
            # ChatOpenAI has native async paths, so wrap them too (wrap_async default).
            return make_adaptive_sampling_cls(ChatOpenAI)(**config)

        # Native mode — AWS SDK via langchain_aws. Imported lazily so gateway-only
        # deployments don't require langchain-aws to be installed. The adaptive subclass
        # auto-drops sampling params a model rejects (e.g. Opus 4.7+ remove temperature)
        # without a hardcoded per-model list.
        try:
            converse_cls = _adaptive_bedrock_converse()
        except ImportError as e:
            raise RuntimeError(
                "Bedrock native mode requires the 'langchain-aws' package. Install it "
                "(pip install langchain-aws) or use gateway mode by setting BEDROCK_BASE_URL."
            ) from e

        # ChatBedrockConverse forbids extra fields (no `streaming` param). Streaming
        # still works via .astream() in the agent path. Mirrors the Vertex provider.
        kwargs.pop("streaming", None)

        logger.info(f"Creating Bedrock (native) chat model: {native_model} (region={self.region})")
        # Per-call read timeout (from `request_timeout`) rides in the botocore Config
        # together with the retry policy; the model's own `timeout` field is left
        # unset on purpose (see _translate_call_options).
        per_call_read_timeout = call_fields.pop("_read_timeout", None)
        config = {
            "model": native_model,
            "temperature": temperature,
            # `global` is a profile geo, not an AWS region — boto3 needs a real one to sign against.
            "region_name": _resolve_client_region(self.region),
            "config": _botocore_retry_config(read_timeout=per_call_read_timeout),
        }
        # Pass explicit credentials only if set; otherwise rely on boto3's default
        # chain (env / shared config / IAM role).
        if self.access_key and self.secret_key:
            config["aws_access_key_id"] = self.access_key
            config["aws_secret_access_key"] = self.secret_key
            if self.session_token:
                config["aws_session_token"] = self.session_token
        if self.profile:
            config["credentials_profile_name"] = self.profile

        config.update(call_fields)
        config.update(kwargs)
        chat_model = converse_cls(**config)
        # langchain-aws ships a per-model profile; for the Claude 5.x generation it
        # reports `temperature: False` and the client drops the value with a
        # warnings.warn() on EVERY call. Aurora hardcodes temperature=0.4, so clear it
        # up front for those models (the adaptive sampling guard still covers models
        # whose profile is missing or wrong).
        model_profile = getattr(chat_model, "profile", None) or {}
        if model_profile.get("temperature") is False and getattr(chat_model, "temperature", None) is not None:
            chat_model.temperature = None
        return chat_model

    def is_available(self) -> bool:
        """Available when a gateway URL is set or a native region is resolvable."""
        return bool(self.base_url) or bool(self.region)

    def supports_model(self, model: str) -> bool:
        """Bedrock serves explicit ``bedrock/`` ids and Anthropic Claude models (so a
        clean ``anthropic/claude-*`` pick routes here under LLM_PROVIDER_MODE=bedrock).
        Anything else (OpenAI/Google/Gemini, or another prefix like ``openrouter/``)
        falls back to its own provider — match by prefix, not a loose substring."""
        prefix = model.split("/", 1)[0] if "/" in model else ""
        if prefix in ("bedrock", "anthropic"):
            return True
        # Bare canonical Claude name with no provider prefix, e.g. "claude-sonnet-4-6".
        return prefix == "" and model.lower().startswith("claude")

    def get_native_model_name(self, model: str) -> str:
        # Explicit bedrock id -> use the suffix as-is.
        if "/" in model and model.split("/", 1)[0] == "bedrock":
            return model.split("/", 1)[1]
        # Clean Anthropic id -> Bedrock inference-profile id. Runs in both native and gateway
        # mode: a Bedrock-fronting gateway still expects profile ids, not clean names. Geo
        # prefix defaults to "us" (override with BEDROCK_REGION for eu/apac).
        anth = ModelMapper.get_native_name(model, "anthropic")
        key = anth.replace(".", "-").lower()
        if key.startswith("claude"):
            base = _ANTHROPIC_TO_BEDROCK_BASE.get(key, f"anthropic.{key}")
            geo = _geo_prefix_for(self.region)
            # No eu./apac. profile exists for these — use the globally-listed one instead
            # of deriving an id AWS would 404 on.
            if geo not in ("us", _GLOBAL_GEO) and key in _GLOBAL_ONLY_GEO_MODELS:
                logger.info(
                    "Bedrock has no '%s.' inference profile for %s; using the 'global.' profile.",
                    geo, key,
                )
                geo = _GLOBAL_GEO
            return f"{geo}.{base}"
        # Not a recognized Anthropic model — pass through (Bedrock will surface a clear error).
        return model

    def get_supported_models(self) -> list[str]:
        return []
