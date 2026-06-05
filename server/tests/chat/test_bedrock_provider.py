"""Amazon Bedrock direct provider compatibility tests."""

import importlib.util
import os
import sys
import types

import pytest


_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROVIDER_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "providers", "bedrock_provider.py"
)


@pytest.fixture()
def bedrock_provider_module(monkeypatch):
    """Load the provider with minimal dependency stubs."""
    for key in (
        "BEDROCK_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "BEDROCK_ENDPOINT_URL",
        "BEDROCK_CREDENTIALS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "BEDROCK_MODEL_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)

    langchain_aws = types.ModuleType("langchain_aws")
    langchain_core = types.ModuleType("langchain_core")
    language_models = types.ModuleType("langchain_core.language_models")
    chat_models = types.ModuleType("langchain_core.language_models.chat_models")

    class ChatBedrockConverse:
        def __init__(self, **config):
            self.config = config

    class BaseChatModel:
        pass

    langchain_aws.ChatBedrockConverse = ChatBedrockConverse
    chat_models.BaseChatModel = BaseChatModel

    base_provider = types.ModuleType("chat.backend.agent.providers.base_provider")

    class BaseLLMProvider:
        def __init__(self):
            self.provider_name = "bedrock"

    base_provider.BaseLLMProvider = BaseLLMProvider

    monkeypatch.setitem(sys.modules, "langchain_aws", langchain_aws)
    monkeypatch.setitem(sys.modules, "langchain_core", langchain_core)
    monkeypatch.setitem(sys.modules, "langchain_core.language_models", language_models)
    monkeypatch.setitem(
        sys.modules, "langchain_core.language_models.chat_models", chat_models
    )
    monkeypatch.setitem(
        sys.modules, "chat.backend.agent.providers.base_provider", base_provider
    )

    spec = importlib.util.spec_from_file_location(
        "chat.backend.agent.providers.bedrock_provider", _PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_builds_bedrock_converse_model_from_env(bedrock_provider_module, monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("BEDROCK_ENDPOINT_URL", "https://bedrock-runtime.example.com")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "test-session-token")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-api-key")

    provider = bedrock_provider_module.BedrockProvider()
    model = provider.get_chat_model(
        "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        temperature=0.2,
        streaming=True,
        callbacks=["callback"],
    )

    assert provider.is_available()
    assert model.config["model"] == "anthropic.claude-3-5-sonnet-20240620-v1:0"
    assert model.config["temperature"] == 0.2
    assert model.config["region_name"] == "us-west-2"
    assert model.config["base_url"] == "https://bedrock-runtime.example.com"
    assert model.config["aws_access_key_id"] == "test-access-key"
    assert model.config["aws_secret_access_key"] == "test-secret-key"
    assert model.config["aws_session_token"] == "test-session-token"
    assert model.config["callbacks"] == ["callback"]
    assert "streaming" not in model.config
    assert "api_key" not in model.config


def test_available_with_region_only_for_iam_roles(bedrock_provider_module, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    provider = bedrock_provider_module.BedrockProvider()

    assert provider.is_available()


def test_partial_static_credentials_are_rejected(bedrock_provider_module, monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")

    provider = bedrock_provider_module.BedrockProvider()

    assert not provider.is_available()
    with pytest.raises(RuntimeError, match="Bedrock provider is not available"):
        provider.get_chat_model("bedrock/anthropic.claude-3-haiku-20240307-v1:0")


def test_arn_models_can_pass_provider_hint(bedrock_provider_module, monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("BEDROCK_MODEL_PROVIDER", "anthropic")

    provider = bedrock_provider_module.BedrockProvider()
    model = provider.get_chat_model(
        "bedrock/arn:aws:bedrock:us-west-2:123456789012:provisioned-model/example"
    )

    assert model.config["model"].startswith("arn:aws:bedrock:")
    assert model.config["provider"] == "anthropic"
