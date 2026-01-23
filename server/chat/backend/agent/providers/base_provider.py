"""
Base provider abstract class for LLM provider implementations.

This module defines the interface that all LLM provider implementations must follow.
Each provider is responsible for creating properly configured LangChain chat model instances.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from langchain_core.language_models.chat_models import BaseChatModel
import logging

logger = logging.getLogger(__name__)


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
