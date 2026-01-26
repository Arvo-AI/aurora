"""
Model name mapper for converting between OpenRouter format and native provider formats.

This module handles translation of model names between different formats:
- OpenRouter format: "provider/model-name" (e.g., "openai/gpt-5.2")
- Native provider formats: Provider-specific model identifiers

The mapper supports bidirectional translation and automatic provider detection.
"""

from typing import Dict, Optional, Tuple
import re

# Model name mappings from OpenRouter format to native provider formats
MODEL_MAPPINGS = {
    "openai/gpt-5.2": {
        "openrouter": "openai/gpt-5.2",
        "openai": "gpt-5.2",
        "provider": "openai",
    },
    "anthropic/claude-sonnet-4-5": {
        "openrouter": "anthropic/claude-sonnet-4.5",  # OpenRouter uses dot, not dash
        "anthropic": "claude-sonnet-4-5",
        "provider": "anthropic",
    },
    # Alias for OpenRouter format (with dot) - maps to same canonical name
    "anthropic/claude-sonnet-4.5": {
        "openrouter": "anthropic/claude-sonnet-4.5",
        "anthropic": "claude-sonnet-4-5",
        "provider": "anthropic",
    },
    "anthropic/claude-opus-4-5": {
        "openrouter": "anthropic/claude-opus-4.5",  # OpenRouter uses dot, not dash
        "anthropic": "claude-opus-4-5",
        "provider": "anthropic",
    },
    # Alias for OpenRouter format (with dot) - maps to same canonical name
    "anthropic/claude-opus-4.5": {
        "openrouter": "anthropic/claude-opus-4.5",
        "anthropic": "claude-opus-4-5",
        "provider": "anthropic",
    },
    "google/gemini-3-pro-preview": {
        "openrouter": "google/gemini-3-pro-preview",
        "google": "gemini-3-pro-preview",
        "provider": "google",
    },
    "anthropic/claude-3-haiku": {
        "openrouter": "anthropic/claude-3-haiku",
        "anthropic": "claude-3-haiku-20240307",
        "provider": "anthropic",
    },
}

# Reverse mapping: native model name -> OpenRouter format
_NATIVE_TO_OPENROUTER = {}
for or_name, mappings in MODEL_MAPPINGS.items():
    for provider, native_name in mappings.items():
        if provider not in ("openrouter", "provider"):
            _NATIVE_TO_OPENROUTER[(provider, native_name)] = or_name


class ModelMapper:
    """Utility class for converting between model name formats."""

    @staticmethod
    def detect_provider(model_name: str) -> Optional[str]:
        """
        Detect which provider a model name belongs to.

        Args:
            model_name: Model name in any format

        Returns:
            Provider name ('openai', 'anthropic', 'google', 'openrouter') or None
        """
        # Check if it's in OpenRouter format (provider/model)
        if "/" in model_name:
            provider_prefix = model_name.split("/")[0]
            provider_map = {
                "openai": "openai",
                "anthropic": "anthropic",
                "google": "google",
            }
            return provider_map.get(provider_prefix, "openrouter")

        # Check native format mappings
        for mappings in MODEL_MAPPINGS.values():
            if model_name in [
                mappings.get("openai"),
                mappings.get("anthropic"),
                mappings.get("google"),
            ]:
                return mappings.get("provider")

        # Default to OpenRouter for unknown models
        return "openrouter"

    @staticmethod
    def get_native_name(model_name: str, target_provider: str) -> str:
        """
        Convert a model name to the target provider's native format.

        Args:
            model_name: Model name in any format (OpenRouter or native)
            target_provider: Target provider ('openai', 'anthropic', 'google', 'openrouter')

        Returns:
            Model name in the target provider's native format

        Examples:
            >>> ModelMapper.get_native_name('openai/gpt-5.2', 'openai')
            'gpt-5.2'
            >>> ModelMapper.get_native_name('gpt-5.2', 'openrouter')
            'openai/gpt-5.2'
        """
        # If it's already in OpenRouter format and we have a mapping
        if model_name in MODEL_MAPPINGS:
            mapping = MODEL_MAPPINGS[model_name]
            return mapping.get(target_provider, model_name)

        # Try reverse lookup (native -> OpenRouter -> target)
        source_provider = ModelMapper.detect_provider(model_name)
        if source_provider:
            or_name = _NATIVE_TO_OPENROUTER.get((source_provider, model_name))
            if or_name and or_name in MODEL_MAPPINGS:
                return MODEL_MAPPINGS[or_name].get(target_provider, model_name)

        # If no mapping found, return as-is (passthrough)
        return model_name

    @staticmethod
    def get_openrouter_name(model_name: str) -> str:
        """
        Convert a native model name to OpenRouter format.

        Args:
            model_name: Native model name

        Returns:
            Model name in OpenRouter format (provider/model)
        """
        if "/" in model_name:
            # Already in OpenRouter format
            return model_name

        # Reverse lookup
        provider = ModelMapper.detect_provider(model_name)
        if provider:
            or_name = _NATIVE_TO_OPENROUTER.get((provider, model_name))
            if or_name:
                return or_name

        # If no mapping found, return as-is
        return model_name

    @staticmethod
    def split_provider_model(model_name: str) -> Tuple[Optional[str], str]:
        """
        Split a model name into provider and model parts.

        Args:
            model_name: Model name in OpenRouter format or native format

        Returns:
            Tuple of (provider, model_name_without_provider)

        Examples:
            >>> ModelMapper.split_provider_model('openai/gpt-5.2')
            ('openai', 'gpt-5.2')
            >>> ModelMapper.split_provider_model('gpt-5.2')
            ('openai', 'gpt-5.2')  # Detected via mapping
        """
        if "/" in model_name:
            parts = model_name.split("/", 1)
            return parts[0], parts[1]

        # Try to detect provider from native name
        provider = ModelMapper.detect_provider(model_name)
        return provider, model_name

    @staticmethod
    def is_model_supported_by_provider(model_name: str, provider: str) -> bool:
        """
        Check if a model is supported by a specific provider.

        Args:
            model_name: Model name in any format
            provider: Provider name to check

        Returns:
            True if the provider supports this model
        """
        detected_provider = ModelMapper.detect_provider(model_name)
        if detected_provider == provider:
            return True

        # Also check if there's a mapping available
        or_name = (
            model_name
            if "/" in model_name
            else ModelMapper.get_openrouter_name(model_name)
        )
        if or_name in MODEL_MAPPINGS:
            return provider in MODEL_MAPPINGS[or_name]

        return False

    @staticmethod
    def get_supported_models_for_provider(provider: str) -> list[str]:
        """
        Get list of models supported by a provider in OpenRouter format.

        Args:
            provider: Provider name

        Returns:
            List of model names in OpenRouter format
        """
        supported = []
        for or_name, mappings in MODEL_MAPPINGS.items():
            if provider in mappings or mappings.get("provider") == provider:
                supported.append(or_name)
        return supported
