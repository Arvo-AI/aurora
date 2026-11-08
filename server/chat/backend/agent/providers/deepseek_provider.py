"""DeepSeek Flash through DeepSeek's Anthropic-compatible Messages API."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel

from .base_provider import BaseLLMProvider
from ..model_mapper import ModelMapper


class ChatDeepSeek(ChatAnthropic):
    """Use named tool calls for Aurora's structured internal decisions."""

    def with_structured_output(
        self, schema, *, include_raw=False, method="function_calling", **kwargs
    ):
        if method != "function_calling":
            raise ValueError("DeepSeek Messages structured output requires function_calling")
        # DeepSeek rejects named tool_choice with thinking enabled, and ignores
        # output_config.format. Disable thinking only on this structured call;
        # keep the investigation model and its clients/history unchanged.
        structured_model = self.model_copy(update={"thinking": {"type": "disabled"}})
        return super(ChatDeepSeek, structured_model).with_structured_output(
            schema, include_raw=include_raw, method=method, **kwargs
        )


class DeepSeekProvider(BaseLLMProvider):
    """Keep DeepSeek credentials and model identity separate from Claude."""

    def __init__(self):
        super().__init__()
        self.api_key = os.getenv("DEEPSEEK_API_KEY")

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        if not self.is_available():
            raise RuntimeError("DeepSeek is not configured. Set DEEPSEEK_API_KEY.")
        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by DeepSeek")

        config = {
            "model": self.get_native_model_name(model),
            "anthropic_api_url": "https://api.deepseek.com/anthropic",
            "anthropic_api_key": self.api_key,
            "max_tokens": 8192,
            "timeout": 120.0,
            "thinking": {"type": "enabled"},
        }
        config.update(kwargs)
        # DeepSeek ignores temperature in thinking mode. Preserve the attribute
        # because the agent reads it when creating its streaming client.
        config["temperature"] = temperature
        return ChatDeepSeek(**config)

    def is_available(self) -> bool:
        return bool(self.api_key)

    def supports_model(self, model: str) -> bool:
        return self.get_native_model_name(model) == "deepseek-flash"

    def get_native_model_name(self, model: str) -> str:
        return ModelMapper.get_native_name(model, "deepseek")

    def get_supported_models(self) -> list[str]:
        return ["deepseek/deepseek-flash"]
