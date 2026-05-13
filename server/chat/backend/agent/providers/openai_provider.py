"""
OpenAI provider implementation for direct API access.

Uses the official OpenAI API instead of going through OpenRouter.
Requires OPENAI_API_KEY environment variable.
"""

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseLLMProvider):
    """Direct OpenAI API provider."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("OPENAI_API_KEY")
        # OpenAI uses their default base URL (no need to specify)

    def get_chat_model(
        self,
        model: str,
        temperature: float = 0.4,
        **kwargs
    ) -> BaseChatModel:
        """
        Return a configured ChatOpenAI instance for direct OpenAI API access.

        Args:
            model: Model name (accepts both OpenRouter format and native format)
            temperature: Temperature setting (default 0.4)
            **kwargs: Additional parameters

        Returns:
            Configured ChatOpenAI instance

        Raises:
            RuntimeError: If OpenAI API key is not configured
            ValueError: If model is not supported by OpenAI
        """
        if not self.is_available():
            raise RuntimeError("OpenAI provider is not available. Please set OPENAI_API_KEY.")

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by OpenAI provider")

        # Convert to native OpenAI format
        native_model = ModelMapper.get_native_name(model, 'openai')

        logger.info(f"Creating OpenAI chat model: {native_model}")

        config = {
            "model": native_model,
            "temperature": temperature,
            "openai_api_key": self.api_key,
            "request_timeout": 120.0,
            "max_retries": 3,
            "stream_usage": True,
        }

        # Reasoning models (GPT-5+, o-series) require the Responses API
        # (/v1/responses) when tools are combined with reasoning effort —
        # Chat Completions returns 400 for that combination. They also
        # reject the `temperature` parameter outright. `summary: "auto"`
        # surfaces reasoning summaries to the streaming output so they
        # flow into the ThoughtsPanel / sub-agent panes during RCA.
        if self._supports_reasoning(native_model):
            config["reasoning"] = {"effort": "high", "summary": "auto"}
            config["use_responses_api"] = True
            config.pop("temperature", None)
            logger.info(
                f"Enabled reasoning=high+summary=auto + use_responses_api=True for {native_model}"
            )

        config.update(kwargs)

        return ChatOpenAI(**config)

    def is_available(self) -> bool:
        """Check if OpenAI API key is configured."""
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        """
        Check if this is an OpenAI model.

        Args:
            model: Model name to check

        Returns:
            True if this is an OpenAI model
        """
        if "/" in model:
            return model.split("/")[0] == "openai"
        return ModelMapper.is_model_supported_by_provider(model, 'openai')

    def get_native_model_name(self, model: str) -> str:
        """
        Convert model name to OpenAI native format.

        Args:
            model: Model name in any format

        Returns:
            Model name in OpenAI native format
        """
        return ModelMapper.get_native_name(model, 'openai')

    @staticmethod
    def _supports_reasoning(native_model: str) -> bool:
        """Check if an OpenAI model supports the reasoning_effort parameter."""
        name = native_model.lower()
        # Exclude older models that don't support reasoning.
        # Everything else (gpt-5+, o-series, future models) gets it.
        non_reasoning = ("gpt-3", "gpt-4")
        return not name.startswith(non_reasoning)

    def get_supported_models(self) -> list[str]:
        """Get list of OpenAI models in OpenRouter format."""
        return ModelMapper.get_supported_models_for_provider('openai')
