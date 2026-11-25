"""Requesty provider tests."""

import importlib.util
import os
import sys
import types

import pytest


_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROVIDER_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "providers", "requesty_provider.py"
)


@pytest.fixture()
def requesty_provider_module(monkeypatch):
    """Load the provider with minimal dependency stubs and the real ModelMapper."""
    monkeypatch.setenv("REQUESTY_API_KEY", "test-key")

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
            pass  # Stub, real implementation lives in base_provider.py

    base_provider.BaseLLMProvider = BaseLLMProvider

    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.language_models", language_models)
    monkeypatch.setitem(
        sys.modules, "langchain_core.language_models.chat_models", chat_models
    )
    monkeypatch.setitem(
        sys.modules, "chat.backend.agent.providers.base_provider", base_provider
    )

    spec = importlib.util.spec_from_file_location(
        "chat.backend.agent.providers.requesty_provider", _PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chat_model_points_at_requesty(requesty_provider_module):
    provider = requesty_provider_module.RequestyProvider()

    model = provider.get_chat_model("openai/gpt-5.5", streaming=True)

    assert model.config["model"] == "openai/gpt-5.5"
    assert model.config["openai_api_base"] == "https://router.requesty.ai/v1"
    assert model.config["openai_api_key"] == "test-key"
    assert model.config["streaming"] is True


def test_anthropic_dot_ids_map_to_requesty_dash_ids(requesty_provider_module):
    provider = requesty_provider_module.RequestyProvider()

    assert provider.get_native_model_name("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4-6"
    assert provider.get_native_model_name("anthropic/claude-opus-4.7") == "anthropic/claude-opus-4-7"


def test_unmapped_ids_pass_through(requesty_provider_module):
    provider = requesty_provider_module.RequestyProvider()

    assert provider.get_native_model_name("deepseek/deepseek-chat") == "deepseek/deepseek-chat"
    assert provider.get_native_model_name("gpt-5.4-mini") == "gpt-5.4-mini"
    assert provider.supports_model("gpt-5.4-mini")


def test_unavailable_without_key(requesty_provider_module, monkeypatch):
    monkeypatch.delenv("REQUESTY_API_KEY")
    provider = requesty_provider_module.RequestyProvider()

    assert not provider.is_available()
    with pytest.raises(RuntimeError, match="REQUESTY_API_KEY"):
        provider.get_chat_model("openai/gpt-5.5")


def test_requesty_mode_routes_every_model_through_requesty(monkeypatch):
    from chat.backend.agent.providers import ProviderRegistry

    monkeypatch.setenv("REQUESTY_API_KEY", "test-key")
    registry = ProviderRegistry()

    assert registry.resolve_provider_name("anthropic/claude-sonnet-4.6", mode="requesty") == "requesty"
    assert registry.resolve_provider_name("openai/gpt-5.5", mode="requesty") == "requesty"


def test_requesty_mode_without_key_names_the_env_var(monkeypatch):
    from chat.backend.agent.providers import ProviderRegistry

    monkeypatch.delenv("REQUESTY_API_KEY", raising=False)
    registry = ProviderRegistry()

    with pytest.raises(RuntimeError, match="REQUESTY_API_KEY"):
        registry.get_provider_for_model("openai/gpt-5.5", mode="requesty")
