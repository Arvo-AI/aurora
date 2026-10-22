"""
Ollama provider implementation for local LLM inference.

Uses ChatOllama from langchain-ollama.
Connects to Ollama running on the host via OLLAMA_BASE_URL.
No API key required.
"""

import logging
import os
import time

import requests
from langchain_core.language_models.chat_models import BaseChatModel

from chat.backend.agent.call_options import effective_max_tokens, pop_call_options

from .base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)


class OllamaProvider(BaseLLMProvider):
    """Ollama provider for local LLM inference."""

    def __init__(self):
        super().__init__()
        # Default uses host.docker.internal for Docker containers to reach host Ollama
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        self._available_cache = None
        self._available_cache_time = 0
        self._models_cache = None
        self._models_cache_time = 0

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        if not self.is_available():
            raise RuntimeError(
                f"Ollama provider is not available. Ensure Ollama is running at {self.base_url}. "
                "Install Ollama on the host and pull models with `ollama pull <model>`."
            )

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Ollama provider")

        # Import here to avoid import error when langchain-ollama is not installed
        from langchain_ollama import ChatOllama

        native_model = self.get_native_model_name(model)

        logger.info(f"Creating Ollama chat model: {native_model} (base_url={self.base_url})")

        config = {
            "model": native_model,
            "temperature": temperature,
            "base_url": self.base_url,
        }
        # Provider-neutral call options: Ollama spells the output cap `num_predict`;
        # it has no per-request timeout or thinking switch, so those are dropped.
        # Local models are an open set (qwen3 / deepseek-r1 / gpt-oss think by
        # default), so a cap that was sized for a thinking-free answer is dropped
        # when the caller also asked for thinking off — reasoning tokens would count
        # against `num_predict` and could truncate the reply.
        opts = pop_call_options(kwargs)
        max_tokens = effective_max_tokens(opts, native_model)
        if max_tokens is not None:
            config["num_predict"] = max_tokens
        elif opts.max_tokens is not None:
            logger.info(
                "Dropping max_tokens=%s for Ollama model %s: thinking cannot be disabled "
                "per call, so the cap could truncate the answer.",
                opts.max_tokens, native_model,
            )

        config.update(kwargs)

        return ChatOllama(**config)

    def is_available(self) -> bool:
        """Check if Ollama is reachable by hitting /api/tags with a 3s timeout."""
        now = time.time()
        # Cache for 10 seconds (short TTL to avoid prolonged false negatives
        # that silently fall through to another provider)
        if self._available_cache is not None and (now - self._available_cache_time) < 10:
            return self._available_cache

        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            self._available_cache = resp.status_code == 200
        except Exception:
            self._available_cache = False

        self._available_cache_time = now
        return self._available_cache

    def supports_model(self, model: str) -> bool:
        if "/" in model:
            return model.split("/")[0] == "ollama"
        return False

    def get_native_model_name(self, model: str) -> str:
        if "/" in model and model.split("/")[0] == "ollama":
            return model.split("/", 1)[1]
        return model

    def get_supported_models(self) -> list[str]:
        """Fetch installed models from Ollama, cached for 60s."""
        now = time.time()
        if self._models_cache is not None and (now - self._models_cache_time) < 60:
            return self._models_cache

        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                models = [f"ollama/{m['name']}" for m in data.get("models", [])]
                self._models_cache = models
                self._models_cache_time = now
                return models
        except Exception as e:
            logger.warning(f"Failed to fetch Ollama models: {e}")

        return []
