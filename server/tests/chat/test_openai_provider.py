"""OpenAI direct provider compatibility tests."""

import importlib.util
import os
import sys
import types

import pytest


_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROVIDER_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "providers", "openai_provider.py"
)


@pytest.fixture()
def openai_provider_module(monkeypatch):
    """Load the provider with minimal dependency stubs."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    langchain_openai = types.ModuleType("langchain_openai")
    langchain_core = types.ModuleType("langchain_core")
    language_models = types.ModuleType("langchain_core.language_models")
    chat_models = types.ModuleType("langchain_core.language_models.chat_models")

    class ChatOpenAI:
        def __init__(self, **config):
            self.config = config

    class BaseChatModel:
        pass

    langchain_openai.ChatOpenAI = ChatOpenAI
    chat_models.BaseChatModel = BaseChatModel

    base_provider = types.ModuleType("chat.backend.agent.providers.base_provider")

    class BaseLLMProvider:
        def __init__(self):
            pass

    base_provider.BaseLLMProvider = BaseLLMProvider

    model_mapper = types.ModuleType("chat.backend.agent.model_mapper")

    class ModelMapper:
        @staticmethod
        def get_native_name(model_name, target_provider):
            assert target_provider == "openai"
            return model_name.split("/", 1)[1] if "/" in model_name else model_name

        @staticmethod
        def is_model_supported_by_provider(model_name, provider):
            return provider == "openai" and model_name.startswith("gpt-")

        @staticmethod
        def get_supported_models_for_provider(provider):
            return ["openai/gpt-5.5"] if provider == "openai" else []

    model_mapper.ModelMapper = ModelMapper

    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.language_models", language_models)
    monkeypatch.setitem(
        sys.modules, "langchain_core.language_models.chat_models", chat_models
    )
    monkeypatch.setitem(
        sys.modules, "chat.backend.agent.providers.base_provider", base_provider
    )
    monkeypatch.setitem(sys.modules, "chat.backend.agent.model_mapper", model_mapper)

    spec = importlib.util.spec_from_file_location(
        "chat.backend.agent.providers.openai_provider", _PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_direct_openai_does_not_enable_reasoning_effort_by_default(openai_provider_module):
    provider = openai_provider_module.OpenAIProvider()

    model = provider.get_chat_model("openai/gpt-5.5", streaming=True)

    assert model.config["model"] == "gpt-5.5"
    assert model.config["streaming"] is True
    assert "reasoning_effort" not in model.config
