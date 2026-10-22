"""
Google AI provider implementation for Gemini models via Google AI Studio.

Uses the Google Generative AI API (Google AI Studio) for direct access to Gemini models.
Requires GOOGLE_AI_API_KEY environment variable.

Note: This uses Google AI Studio API for direct access to Gemini models.
"""

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from chat.backend.agent.call_options import effective_max_tokens, pop_call_options

from .base_provider import BaseLLMProvider, apply_gemini_thinking_config
from ..model_mapper import ModelMapper

try:
    import google.generativeai as genai
except ImportError:
    genai = None

logger = logging.getLogger(__name__)


class GoogleProvider(BaseLLMProvider):
    """Google AI Studio provider for Gemini models."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("GOOGLE_AI_API_KEY")
        self._resolved_model_cache: dict[str, str] = {}

    def _resolve_model_name(self, native_model: str) -> str:
        if native_model in self._resolved_model_cache:
            return self._resolved_model_cache[native_model]

        if not genai or not self.api_key:
            return native_model

        try:
            genai.configure(api_key=self.api_key)
            available_models = []
            for model in genai.list_models():
                name = getattr(model, "name", None)
                if not name:
                    continue
                methods = getattr(model, "supported_generation_methods", None)
                if methods and "generateContent" not in methods:
                    continue
                if name.startswith("models/"):
                    available_models.append(name.split("/", 1)[1])
                else:
                    available_models.append(name)

            if native_model in available_models:
                resolved_model = native_model
            else:
                candidates = [m for m in available_models if m.startswith(native_model)]
                resolved_model = candidates[0] if candidates else native_model

            if resolved_model != native_model:
                logger.info(
                    "Resolved Google model %s -> %s", native_model, resolved_model
                )
            self._resolved_model_cache[native_model] = resolved_model
            return resolved_model
        except Exception as exc:
            logger.warning("Failed to resolve Google model name: %s", exc)
            return native_model

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        """
        Return a configured ChatGoogleGenerativeAI instance.

        Args:
            model: Model name (accepts both OpenRouter format and native format)
            temperature: Temperature setting (default 0.4)
            **kwargs: Additional parameters

        Returns:
            Configured ChatGoogleGenerativeAI instance

        Raises:
            RuntimeError: If Google AI API key is not configured
            ValueError: If model is not supported by Google AI
        """
        if not self.is_available():
            raise RuntimeError(
                "Google AI provider is not available. Please set GOOGLE_AI_API_KEY."
            )

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Google AI provider")

        # Convert to native Google AI format
        native_model = ModelMapper.get_native_name(model, "google")
        resolved_model = self._resolve_model_name(native_model)

        logger.info(f"Creating Google AI chat model: {resolved_model}")

        # Strip 'streaming' — not a valid ChatGoogleGenerativeAI param in v4.x
        kwargs.pop("streaming", None)

        config = {
            "model": resolved_model,
            "temperature": temperature if temperature is not None else 0.7,
            "google_api_key": self.api_key,
        }

        apply_gemini_thinking_config(config, resolved_model)
        # Provider-neutral call options -> Gemini fields: `max_tokens` is
        # `max_output_tokens`, `request_timeout` is the constructor alias of `timeout`.
        # `disable_thinking` is dropped here — Gemini thinking is governed globally by
        # GEMINI_DISABLE_THINKING (see apply_gemini_thinking_config), and even that
        # only hides the thoughts: a 2.5+/3/4 model still thinks server-side. So a
        # `max_tokens` sized for a thinking-free answer is only forwarded to models
        # that do not think by default (flash-lite, pre-2.5); on thinking models the
        # cap is dropped and the model keeps its default output limit.
        opts = pop_call_options(kwargs)
        max_tokens = effective_max_tokens(opts, resolved_model)
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        elif opts.max_tokens is not None:
            logger.info(
                "Dropping max_tokens=%s for thinking model %s: Gemini thinking cannot be "
                "disabled per call, so the cap could truncate the answer.",
                opts.max_tokens, resolved_model,
            )
        if opts.request_timeout is not None:
            config["request_timeout"] = opts.request_timeout
        config.update(kwargs)

        return ChatGoogleGenerativeAI(**config)

    def is_available(self) -> bool:
        """Check if Google AI API key is configured."""
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        """
        Check if this is a Google Gemini model.

        Args:
            model: Model name to check

        Returns:
            True if this is a Google model
        """
        if "/" in model:
            return model.split("/")[0] == "google"
        return ModelMapper.is_model_supported_by_provider(model, "google")

    def get_native_model_name(self, model: str) -> str:
        """
        Convert model name to Google AI native format.

        Args:
            model: Model name in any format

        Returns:
            Model name in Google AI native format
        """
        return ModelMapper.get_native_name(model, "google")

    def get_supported_models(self) -> list[str]:
        """Get list of Google AI models in OpenRouter format."""
        return ModelMapper.get_supported_models_for_provider("google")
