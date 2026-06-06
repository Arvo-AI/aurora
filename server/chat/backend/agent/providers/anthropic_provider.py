"""
Anthropic provider implementation for direct Claude API access.

Uses the official Anthropic API instead of going through OpenRouter.
Requires ANTHROPIC_API_KEY environment variable.
"""

import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel

from .base_provider import BaseLLMProvider
from ._sampling_guard import strip_rejected_sampling
from ..model_mapper import ModelMapper

logger = logging.getLogger(__name__)

_adaptive_anthropic_cls = None


def _adaptive_chat_anthropic():
    """Return a ``ChatAnthropic`` subclass that self-heals around unsupported sampling
    params.

    Anthropic removed ``temperature`` / ``top_p`` / ``top_k`` for Opus 4.7+ — the direct
    API returns 400 ``"temperature is deprecated for this model"`` when they are sent
    (Sonnet 4.6 and Opus 4.6 still accept them). Aurora hardcodes ``temperature=0.4`` and
    langchain-anthropic forwards it unconditionally, so any Opus 4.7+ call via the direct
    provider fails. Rather than hardcode a per-model list, the subclass reacts to the
    model's own error: on a rejection naming a sampling field, it strips that field and
    retries once, then keeps it off for the life of the (cached) instance.

    Unlike Bedrock's Converse, ``ChatAnthropic`` defines its own async paths, so we wrap
    both sync (``_generate`` / ``_stream``) and async (``_agenerate`` / ``_astream``).
    The deprecation 400 is raised at request time before any tokens stream, so the retry
    never double-emits.
    """
    global _adaptive_anthropic_cls
    if _adaptive_anthropic_cls is not None:
        return _adaptive_anthropic_cls

    class _AdaptiveChatAnthropic(ChatAnthropic):
        """ChatAnthropic that drops sampling params a model rejects, then remembers."""

        def _generate(self, *args, **kwargs):
            try:
                return super()._generate(*args, **kwargs)
            except Exception as err:  # noqa: BLE001
                if not strip_rejected_sampling(self, err, logger):
                    raise
                return super()._generate(*args, **kwargs)

        async def _agenerate(self, *args, **kwargs):
            try:
                return await super()._agenerate(*args, **kwargs)
            except Exception as err:  # noqa: BLE001
                if not strip_rejected_sampling(self, err, logger):
                    raise
                return await super()._agenerate(*args, **kwargs)

        def _stream(self, *args, **kwargs):
            yielded = False
            try:
                for chunk in super()._stream(*args, **kwargs):
                    yielded = True
                    yield chunk
            except Exception as err:  # noqa: BLE001
                # Only retry if nothing streamed yet, else a retry would double-emit.
                if yielded or not strip_rejected_sampling(self, err, logger):
                    raise
                yield from super()._stream(*args, **kwargs)

        async def _astream(self, *args, **kwargs):
            yielded = False
            try:
                async for chunk in super()._astream(*args, **kwargs):
                    yielded = True
                    yield chunk
            except Exception as err:  # noqa: BLE001
                # Only retry if nothing streamed yet, else a retry would double-emit.
                if yielded or not strip_rejected_sampling(self, err, logger):
                    raise
                async for chunk in super()._astream(*args, **kwargs):
                    yield chunk

    _adaptive_anthropic_cls = _AdaptiveChatAnthropic
    return _adaptive_anthropic_cls


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
        config.update(kwargs)

        return _adaptive_chat_anthropic()(**config)

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
