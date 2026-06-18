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

Configuration is environment-variable based, like Ollama and Vertex. Bedrock-
specific ``BEDROCK_*`` vars take precedence over the standard ``AWS_*`` vars.
"""

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base_provider import BaseLLMProvider
from ._sampling_guard import make_adaptive_sampling_cls
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)

# Anthropic native model name (dashed) -> Bedrock inference-profile base (without the
# region/geo prefix). Only models whose Bedrock id carries a version/date suffix that
# can't be derived from the name are listed here; bare models (e.g. claude-opus-4-7)
# derive `anthropic.<name>` automatically. The geo prefix (us./eu./apac.) is prepended
# at runtime from the configured region, so this stays region-agnostic.
_ANTHROPIC_TO_BEDROCK_BASE = {
    "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
    "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _geo_prefix_for(region: str) -> str:
    """Bedrock cross-region inference-profile geo prefix derived from the AWS region."""
    r = (region or "us-east-1").lower()
    if r.startswith("eu"):
        return "eu"
    if r.startswith("ap"):
        return "apac"
    return "us"


def _adaptive_bedrock_converse():
    """Lazily import ``ChatBedrockConverse`` (so gateway-only deployments don't need
    langchain-aws) and wrap it with the shared sampling self-heal. ``wrap_async=False``:
    Converse's async path dispatches to the sync one via ``BaseChatModel``, so wrapping
    async too would retry twice. ``ImportError`` propagates to the caller (kept as a
    friendly RuntimeError there)."""
    from langchain_aws import ChatBedrockConverse

    return make_adaptive_sampling_cls(ChatBedrockConverse, wrap_async=False)


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

        # Native mode (AWS SDK). BEDROCK_* takes precedence over standard AWS_*.
        self.region = (
            os.getenv("BEDROCK_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
        )
        self.access_key = os.getenv("BEDROCK_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_key = os.getenv("BEDROCK_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
        self.session_token = os.getenv("BEDROCK_SESSION_TOKEN") or os.getenv("AWS_SESSION_TOKEN")
        self.profile = os.getenv("BEDROCK_PROFILE")

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
                "max_retries": 3,
                "stream_usage": True,
            }
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
        config = {
            "model": native_model,
            "temperature": temperature,
            "region_name": self.region,
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

        config.update(kwargs)
        return converse_cls(**config)

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
            return f"{_geo_prefix_for(self.region)}.{base}"
        # Not a recognized Anthropic model — pass through (Bedrock will surface a clear error).
        return model

    def get_supported_models(self) -> list[str]:
        return []
