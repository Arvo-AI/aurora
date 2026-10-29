"""
Requesty provider implementation.

Requesty is an OpenAI-compatible LLM gateway that routes to multiple
providers with a single API key.
"""

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class RequestyProvider(BaseLLMProvider):
    """Requesty provider for unified access to multiple LLM providers."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("REQUESTY_API_KEY")
        self.base_url = "https://router.requesty.ai/v1"

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        """
        Return a configured ChatOpenAI instance pointing to Requesty.

        Args:
            model: Model name in provider/model format (e.g., "openai/gpt-5.5")
            temperature: Temperature setting (default 0.4)
            **kwargs: Additional parameters (e.g., streaming, callbacks)

        Returns:
            Configured ChatOpenAI instance

        Raises:
            RuntimeError: If Requesty API key is not configured
        """
        if not self.is_available():
            raise RuntimeError(
                "Requesty provider is not available. Please set REQUESTY_API_KEY."
            )

        requesty_model = ModelMapper.get_native_name(model, "requesty")

        logger.info(f"Creating Requesty chat model: {requesty_model}")

        config = {
            "model": requesty_model,
            "temperature": temperature,
            "openai_api_base": self.base_url,
            "openai_api_key": self.api_key,
            "request_timeout": 120.0,
            "max_retries": 3,
            "stream_usage": True,
        }
        config.update(kwargs)

        return ChatOpenAI(**config)

    def is_available(self) -> bool:
        """Check if Requesty API key is configured."""
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        """
        Requesty supports models from many providers.

        Args:
            model: Model name to check

        Returns:
            True for any model with a detectable provider
        """
        provider = ModelMapper.detect_provider(model)
        return provider is not None

    def get_native_model_name(self, model: str) -> str:
        """
        Convert model name to Requesty format.

        Args:
            model: Model name in any format

        Returns:
            Model name in Requesty format (provider/model)
        """
        return ModelMapper.get_native_name(model, "requesty")

    def get_supported_models(self) -> list[str]:
        """Get list of all models with a known Requesty mapping."""
        return ModelMapper.get_supported_models_for_provider("requesty")
