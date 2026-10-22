"""
Anthropic provider implementation for direct Claude API access.

Uses the official Anthropic API instead of going through OpenRouter.
Requires ANTHROPIC_API_KEY environment variable.
"""

import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel

from chat.backend.agent.call_options import (
    effective_max_tokens,
    pop_call_options,
    supports_thinking_disable,
)

from .base_provider import BaseLLMProvider
from ._sampling_guard import make_adaptive_sampling_cls
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

        config = {
            "model": native_model,
            "temperature": temperature,
            "anthropic_api_key": self.api_key,
            "max_retries": 3,
            "timeout": 30.0,
        }
        # Provider-neutral call options -> ChatAnthropic fields. `request_timeout`
        # becomes `timeout` (alias of default_request_timeout); `disable_thinking`
        # becomes the Messages API `thinking={"type": "disabled"}` block, only for
        # models that document disabling as supported (Opus 5.5 / Sonnet 5.5 / Fable
        # reject it, so the flag is dropped for them). `max_tokens` is only sent when
        # thinking is really off for the call: on a 5.x model that keeps thinking
        # (Opus 5.5 as the MAIN_MODEL fallback for auxiliary calls) a cap sized for a
        # thinking-free answer would be eaten by reasoning tokens, so it is dropped
        # and ChatAnthropic's own default (the model profile, 4096 otherwise) applies
        # — the pre-cap behaviour.
        opts = pop_call_options(kwargs)
        thinking_disabled = opts.disable_thinking and supports_thinking_disable(native_model)
        max_tokens = effective_max_tokens(opts, native_model, thinking_disabled=thinking_disabled)
        if max_tokens is not None:
            config["max_tokens"] = max_tokens
        elif opts.max_tokens is not None:
            logger.info(
                "Dropping max_tokens=%s for %s: thinking cannot be disabled, so a cap "
                "sized for a thinking-free answer could truncate it.",
                opts.max_tokens, native_model,
            )
        if opts.request_timeout is not None:
            config["timeout"] = opts.request_timeout
        if thinking_disabled:
            config["thinking"] = {"type": "disabled"}
        config.update(kwargs)

        # Opus 4.7+ rejects the hardcoded temperature; the adaptive subclass strips
        # rejected sampling params and retries. ChatAnthropic has native async paths,
        # so wrap them too (wrap_async default).
        return make_adaptive_sampling_cls(ChatAnthropic)(**config)

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
        if "/" in model:
            return model.split("/")[0] == "anthropic"
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
