"""
Anthropic provider implementation for direct Claude API access.

Uses the official Anthropic API instead of going through OpenRouter.
Requires ANTHROPIC_API_KEY environment variable.
"""

import os
from typing import Optional
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
import logging

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseLLMProvider):
    """Direct Anthropic API provider for Claude models."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

    def get_chat_model(
        self,
        model: str,
        temperature: float = 0.4,
        **kwargs
    ) -> BaseChatModel:
        """
        Return a configured ChatAnthropic instance for direct Anthropic API access.

        Args:
            model: Model name (accepts both OpenRouter format and native format)
            temperature: Temperature setting (default 0.4)
            **kwargs: Additional parameters

        Returns:
            Configured ChatAnthropic instance

        Raises:
            RuntimeError: If Anthropic API key is not configured
            ValueError: If model is not supported by Anthropic
        """
        if not self.is_available():
            raise RuntimeError("Anthropic provider is not available. Please set ANTHROPIC_API_KEY.")

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Anthropic provider")

        # Convert to native Anthropic format
        native_model = ModelMapper.get_native_name(model, 'anthropic')

        logger.info(f"Creating Anthropic chat model: {native_model}")

        # Build configuration for direct Anthropic access
        config = {
            "model": native_model,
            "temperature": temperature,
            "anthropic_api_key": self.api_key,
            "max_retries": 3,
            "timeout": 30.0,
        }

        # Add any additional kwargs
        # Note: ChatAnthropic may have different parameter names than ChatOpenAI
        config.update(kwargs)

        return ChatAnthropic(**config)

    def is_available(self) -> bool:
        """Check if Anthropic API key is configured."""
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        """
        Check if this is an Anthropic/Claude model.

        Args:
            model: Model name to check

        Returns:
            True if this is an Anthropic model
        """
        return ModelMapper.is_model_supported_by_provider(model, 'anthropic')

    def get_native_model_name(self, model: str) -> str:
        """
        Convert model name to Anthropic native format.

        Args:
            model: Model name in any format

        Returns:
            Model name in Anthropic native format
        """
        return ModelMapper.get_native_name(model, 'anthropic')

    def get_supported_models(self) -> list[str]:
        """Get list of Anthropic models in OpenRouter format."""
        return ModelMapper.get_supported_models_for_provider('anthropic')
