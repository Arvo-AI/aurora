"""
Google AI provider implementation for Gemini models via Google AI Studio.

Uses the Google Generative AI API (Google AI Studio) for direct access to Gemini models.
Requires GOOGLE_AI_API_KEY environment variable.

Note: This uses Google AI Studio API for direct access to Gemini models.
"""

import os
from typing import Optional
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.language_models.chat_models import BaseChatModel
import logging

from .base_provider import BaseLLMProvider
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

        # Build configuration for Google AI Studio access
        config = {
            "model": resolved_model,
            "temperature": temperature if temperature is not None else 0.7,
            "google_api_key": self.api_key,
        }

        # Enable thinking for thinking-capable models (Gemini 2.5+, Gemini 3+)
        # Can be disabled via GEMINI_DISABLE_THINKING=true for debugging
        disable_thinking = os.getenv("GEMINI_DISABLE_THINKING", "").lower() in (
            "true",
            "1",
            "yes",
        )
        is_thinking_model = not disable_thinking and any(
            x in resolved_model.lower()
            for x in [
                "gemini-3",
                "gemini-2.5",
                "2.5-pro",
                "2.5-flash",
                "3-pro",
                "3-flash",
            ]
        )
        if is_thinking_model:
            config["include_thoughts"] = True
            config["thinking_level"] = "high"
            logger.info(
                f"Enabled thinking mode for {resolved_model} (include_thoughts=True, thinking_level=high)"
            )
        elif disable_thinking:
            logger.info(
                f"Thinking mode disabled via GEMINI_DISABLE_THINKING for {resolved_model}"
            )

        # Add any additional kwargs
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
