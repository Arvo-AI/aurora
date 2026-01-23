"""
LLM Provider Registry and Factory.

This module provides a registry of all available LLM providers and a factory
for creating chat model instances based on provider selection strategy.

Provider Selection Modes:
- 'direct': Use direct provider APIs based on model prefix (default)
- 'auto': Same as direct - resolve provider from model, no fallback
- 'openrouter': Use OpenRouter for all models (explicit only)

Note: OpenRouter is NOT a fallback. If direct provider is unavailable,
the call will fail with a clear error message.

Environment Variables:
- LLM_PROVIDER_MODE: Selection strategy (default: 'direct')
- OPENROUTER_API_KEY: OpenRouter API key (only needed if mode=openrouter)
- OPENAI_API_KEY: Direct OpenAI API key
- ANTHROPIC_API_KEY: Direct Anthropic API key
- GOOGLE_AI_API_KEY: Google AI Studio API key
"""

import os
from typing import Optional, Dict, List
from langchain_core.language_models.chat_models import BaseChatModel
import logging

from .base_provider import BaseLLMProvider
from .openrouter_provider import OpenRouterProvider
from .openai_provider import OpenAIProvider
from .anthropic_provider import AnthropicProvider
from .google_provider import GoogleProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry of all available LLM providers."""

    def __init__(self):
        """Initialize the provider registry."""
        self._providers: Dict[str, BaseLLMProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize all provider instances."""
        # OpenRouter provider (explicit mode only, not a fallback)
        self._providers["openrouter"] = OpenRouterProvider()

        # Initialize direct providers
        self._providers["openai"] = OpenAIProvider()
        self._providers["anthropic"] = AnthropicProvider()
        self._providers["google"] = GoogleProvider()

        logger.info("Initialized provider registry")

    def get_provider(self, provider_name: str) -> Optional[BaseLLMProvider]:
        """
        Get a provider by name.

        Args:
            provider_name: Name of the provider

        Returns:
            Provider instance or None if not found
        """
        return self._providers.get(provider_name)

    def get_available_providers(self) -> Dict[str, BaseLLMProvider]:
        """
        Get all providers that are currently available (have valid credentials).

        Returns:
            Dictionary of available provider instances
        """
        available = {}
        for name, provider in self._providers.items():
            if provider.is_available():
                available[name] = provider
        return available

    def get_provider_for_model(
        self, model: str, mode: str = "direct"
    ) -> BaseLLMProvider:
        """
        Get the appropriate provider for a model based on selection mode.

        Args:
            model: Model name (e.g., 'anthropic/claude-opus-4.5')
            mode: Provider selection mode ('direct', 'auto', 'openrouter')

        Returns:
            Provider instance

        Raises:
            RuntimeError: If no suitable provider is available
        """
        if mode == "openrouter":
            # Explicit OpenRouter mode - use OpenRouter for everything
            provider = self._providers["openrouter"]
            if not provider.is_available():
                raise RuntimeError(
                    "OpenRouter provider is not available (missing OPENROUTER_API_KEY)"
                )
            return provider

        # For 'direct' and 'auto' modes: resolve provider from model prefix
        # No OpenRouter fallback - if direct provider unavailable, fail with clear error
        detected_provider = ModelMapper.detect_provider(model)

        if mode in ("direct", "auto"):
            if not detected_provider or detected_provider == "openrouter":
                # Model has no known direct provider mapping
                raise RuntimeError(
                    f"Model '{model}' has no direct provider mapping. "
                    f"Use mode='openrouter' to route through OpenRouter, or check model name format (e.g., 'anthropic/claude-opus-4.5')."
                )

            provider = self._providers.get(detected_provider)
            if provider and provider.is_available():
                logger.info(
                    f"Using {detected_provider} provider for model {model} (mode={mode})"
                )
                return provider

            # Provider exists but not available (missing credentials)
            env_var_hints = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_AI_API_KEY",
            }
            hint = env_var_hints.get(
                detected_provider, f"{detected_provider.upper()}_API_KEY"
            )
            raise RuntimeError(
                f"Provider '{detected_provider}' is not available for model '{model}'. "
                f"Configure {hint} or set LLM_PROVIDER_MODE=openrouter to use OpenRouter instead."
            )

        else:
            raise ValueError(
                f"Invalid provider mode: {mode}. Use 'direct', 'auto', or 'openrouter'."
            )

    def get_provider_info(self) -> List[Dict]:
        """
        Get information about all providers.

        Returns:
            List of provider information dictionaries
        """
        info = []
        for name, provider in self._providers.items():
            info.append(
                {
                    "name": name,
                    "available": provider.is_available(),
                    "class": provider.__class__.__name__,
                }
            )
        return info


# Global provider registry instance
_registry = None


def get_registry() -> ProviderRegistry:
    """
    Get the global provider registry instance.

    Returns:
        The global ProviderRegistry instance
    """
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def create_chat_model(
    model: str, temperature: float = 0.4, provider_mode: Optional[str] = None, **kwargs
) -> BaseChatModel:
    """
    Factory function to create a chat model instance.

    Args:
        model: Model name (in any format)
        temperature: Temperature setting (default 0.4)
        provider_mode: Provider selection mode (default from env LLM_PROVIDER_MODE)
        **kwargs: Additional parameters to pass to the provider

    Returns:
        Configured LangChain chat model instance

    Raises:
        RuntimeError: If no suitable provider is available

    Examples:
        >>> # Use direct provider (default) - resolves from model prefix
        >>> model = create_chat_model("anthropic/claude-opus-4.5")  # Uses Anthropic API

        >>> # Explicit OpenRouter mode
        >>> model = create_chat_model("openai/gpt-5", provider_mode="openrouter")
    """
    # Get provider mode from environment if not specified
    if provider_mode is None:
        provider_mode = os.getenv("LLM_PROVIDER_MODE")

    logger.info(f"Creating chat model: {model} (mode: {provider_mode})")

    # Get the appropriate provider
    registry = get_registry()
    provider = registry.get_provider_for_model(model, mode=provider_mode)

    # Create and return the chat model
    return provider.get_chat_model(model, temperature=temperature, **kwargs)


def get_available_providers() -> Dict[str, bool]:
    """
    Get status of all providers.

    Returns:
        Dictionary mapping provider names to availability status

    Example:
        >>> get_available_providers()
        {'openrouter': True, 'openai': False, 'anthropic': True, ...}
    """
    registry = get_registry()
    return {
        name: provider.is_available() for name, provider in registry._providers.items()
    }


# Export key classes and functions
__all__ = [
    "BaseLLMProvider",
    "OpenRouterProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "GoogleProvider",
    "ProviderRegistry",
    "get_registry",
    "create_chat_model",
    "get_available_providers",
]
