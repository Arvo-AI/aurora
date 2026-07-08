"""
OpenRouter provider implementation.

OpenRouter acts as a unified gateway to multiple LLM providers.
This provider maintains backward compatibility with the existing Aurora setup.
"""

import logging
import os
import time

import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class OpenRouterProvider(BaseLLMProvider):
    """OpenRouter provider for unified access to multiple LLM providers."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        self.base_url = "https://openrouter.ai/api/v1"
        # Cache the live model catalog (OpenRouter serves hundreds) for 10 min so
        # the selector endpoint doesn't hit the network on every request.
        self._models_cache: list[str] | None = None
        self._models_cache_time: float = 0.0

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

        config = {
            "model": openrouter_model,
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
        """Get the list of models available via OpenRouter.

        Fetches OpenRouter's live ``/models`` catalog (hundreds of models across
        every upstream provider), cached for 10 minutes. Falls back to the
        statically mapped set if the API is unreachable or the key is unset, so
        the selector always has something to show.
        """
        now = time.time()
        if self._models_cache is not None and (now - self._models_cache_time) < 600:
            return self._models_cache

        mapped = ModelMapper.get_supported_models_for_provider("openrouter")
        if not self.api_key:
            return mapped

        try:
            resp = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                # OpenRouter ids are already in "provider/model" form.
                live = [m["id"] for m in data if m.get("id")]
                if live:
                    self._models_cache = live
                    self._models_cache_time = now
                    return live
            else:
                logger.warning("OpenRouter /models returned %s", resp.status_code)
        except Exception as e:
            logger.warning("Failed to fetch OpenRouter models: %s", e)

        return mapped
