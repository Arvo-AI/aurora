"""
OpenRouter provider implementation.

OpenRouter acts as a unified gateway to multiple LLM providers.
This provider maintains backward compatibility with the existing Aurora setup.
"""

import os
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel
import logging

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class OpenRouterProvider(BaseLLMProvider):
    """OpenRouter provider for unified access to multiple LLM providers."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.base_url = "https://openrouter.ai/api/v1"

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        """
        Return a configured ChatOpenAI instance pointing to OpenRouter.

        Args:
            model: Model name in OpenRouter format (e.g., "openai/gpt-5")
            temperature: Temperature setting (default 0.4)
            **kwargs: Additional parameters (e.g., extra_body, streaming)

        Returns:
            Configured ChatOpenAI instance

        Raises:
            RuntimeError: If OpenRouter API key is not configured
        """
        if not self.is_available():
            raise RuntimeError(
                "OpenRouter provider is not available. Please set OPENROUTER_API_KEY."
            )

        # Convert to OpenRouter format if needed
        openrouter_model = ModelMapper.get_native_name(model, "openrouter")

        logger.info(f"Creating OpenRouter chat model: {openrouter_model}")

        # Build configuration
        config = {
            "model": openrouter_model,
            "temperature": temperature,
            "openai_api_base": self.base_url,
            "openai_api_key": self.api_key,
            "request_timeout": 120.0,
            "max_retries": 3,
        }

        # Add any additional kwargs
        config.update(kwargs)

        return ChatOpenAI(**config)

    def is_available(self) -> bool:
        """Check if OpenRouter API key is configured."""
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        """
        OpenRouter supports many models. Check if we have a mapping for it.

        Args:
            model: Model name to check

        Returns:
            True - OpenRouter supports a wide range of models
        """
        # OpenRouter supports most models, so we return True broadly
        # However, we can check if we have explicit mapping
        provider = ModelMapper.detect_provider(model)
        return provider is not None

    def get_native_model_name(self, model: str) -> str:
        """
        Convert model name to OpenRouter format.

        Args:
            model: Model name in any format

        Returns:
            Model name in OpenRouter format (provider/model)
        """
        return ModelMapper.get_native_name(model, "openrouter")

    def get_supported_models(self) -> list[str]:
        """Get list of all models available via OpenRouter."""
        return ModelMapper.get_supported_models_for_provider("openrouter")
