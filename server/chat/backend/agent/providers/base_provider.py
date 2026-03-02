"""
Base provider abstract class for LLM provider implementations.

This module defines the interface that all LLM provider implementations must follow.
Each provider is responsible for creating properly configured LangChain chat model instances.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

# Gemini model name fragments that indicate thinking support (2.5+, 3+)
_GEMINI_THINKING_INDICATORS = (
    "gemini-3", "gemini-2.5",
    "2.5-pro", "2.5-flash",
    "3-pro", "3-flash",
)


def apply_gemini_thinking_config(config: dict, model_name: str) -> None:
    """Apply thinking mode configuration for Gemini models that support it.

    Mutates the config dict in place to add `include_thoughts` and `thinking_level`
    when the model supports thinking and it is not disabled via env var.

    Args:
        config: Model configuration dict to update.
        model_name: Native Gemini model name (e.g. "gemini-2.5-pro").
    """
    disable_thinking = os.getenv("GEMINI_DISABLE_THINKING", "").lower() in (
        "true", "1", "yes",
    )
    if disable_thinking:
        logger.info(f"Thinking mode disabled via GEMINI_DISABLE_THINKING for {model_name}")
        return

    is_thinking_model = any(
        indicator in model_name.lower() for indicator in _GEMINI_THINKING_INDICATORS
    )
    if is_thinking_model:
        config["include_thoughts"] = True
        config["thinking_level"] = "high"
        logger.info(f"Enabled thinking mode for {model_name}")


class BaseLLMProvider(ABC):
    """Abstract base class for LLM provider implementations."""

    def __init__(self):
        """Initialize the provider."""
        self.provider_name = self.__class__.__name__.replace("Provider", "").lower()

    @abstractmethod
    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        """
        Return a configured LangChain chat model instance.

        Args:
            model: The model identifier (provider-specific format)
            temperature: Temperature setting for the model (default 0.4)
            **kwargs: Additional provider-specific parameters

        Returns:
            A configured LangChain BaseChatModel instance

        Raises:
            ValueError: If the model is not supported by this provider
            RuntimeError: If the provider is not properly configured
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if this provider has valid API credentials and is ready to use.

        Returns:
            True if the provider is available, False otherwise
        """
        pass

    @abstractmethod
    def supports_model(self, model: str) -> bool:
        """
        Check if this provider supports the given model identifier.

        Args:
            model: The model identifier to check

        Returns:
            True if the provider supports this model, False otherwise
        """
        pass

    @abstractmethod
    def get_native_model_name(self, model: str) -> str:
        """
        Convert a model identifier to the provider's native model name format.

        Args:
            model: The model identifier (may be in OpenRouter format or native format)

        Returns:
            The provider's native model name

        Examples:
            - OpenRouter format: "openai/gpt-5" -> "gpt-5"
            - OpenRouter format: "anthropic/claude-sonnet-4.5" -> "claude-4.5-sonnet-20250929"
            - Native format: "gpt-5" -> "gpt-5" (passthrough)
        """
        pass

    def get_provider_info(self) -> Dict[str, Any]:
        """
        Get information about this provider.

        Returns:
            Dictionary containing provider metadata
        """
        return {
            "name": self.provider_name,
            "available": self.is_available(),
            "supported_models": self.get_supported_models()
            if hasattr(self, "get_supported_models")
            else [],
        }

    def validate_configuration(self) -> tuple[bool, Optional[str]]:
        """
        Validate the provider's configuration.

        Returns:
            Tuple of (is_valid, error_message)
            - is_valid: True if configuration is valid
            - error_message: None if valid, error description if invalid
        """
        if not self.is_available():
            return (
                False,
                f"{self.provider_name} provider is not available (missing API key or configuration)",
            )
        return True, None
