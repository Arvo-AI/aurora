"""Shared chat-model instance cache (chat.backend.agent.providers.get_cached_chat_model).

Auxiliary callers (judge, summaries, memory selector/agent, severity) used to
build a fresh client — two boto3 clients each on Bedrock — per call. The cache
hands out one instance per distinct construction signature and refuses to
cache anything that carries per-request state (callbacks, streaming).
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain_openai")

from chat.backend.agent import providers  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    providers.clear_chat_model_cache()
    monkeypatch.delenv("LLM_PROVIDER_MODE", raising=False)
    yield
    providers.clear_chat_model_cache()


@pytest.fixture
def factory(monkeypatch):
    counter = {"n": 0}

    def _build(model, temperature=0.4, provider_mode=None, **kwargs):
        counter["n"] += 1
        return MagicMock(name=f"model-{counter['n']}", model_id=model, temperature=temperature, kwargs=kwargs)

    spy = MagicMock(side_effect=_build)
    monkeypatch.setattr(providers, "create_chat_model", spy)
    return spy


def test_same_signature_returns_the_same_instance(factory):
    a = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, max_tokens=400, disable_thinking=True)
    b = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, disable_thinking=True, max_tokens=400)
    assert a is b
    assert factory.call_count == 1


@pytest.mark.parametrize(
    "variant",
    [
        {"model": "anthropic/claude-opus-5.5", "temperature": 0.0, "max_tokens": 400},
        {"model": "anthropic/claude-haiku-5.5", "temperature": 0.3, "max_tokens": 400},
        {"model": "anthropic/claude-haiku-5.5", "temperature": 0.0, "max_tokens": 32},
        {"model": "anthropic/claude-haiku-5.5", "temperature": 0.0, "max_tokens": 400, "request_timeout": 25},
        {"model": "anthropic/claude-haiku-5.5", "temperature": 0.0, "max_tokens": 400, "disable_thinking": True},
    ],
)
def test_any_option_change_builds_a_new_instance(factory, variant):
    base = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, max_tokens=400)
    model = variant.pop("model")
    temperature = variant.pop("temperature")
    other = providers.get_cached_chat_model(model, temperature=temperature, **variant)
    assert other is not base
    assert factory.call_count == 2


def test_provider_mode_is_part_of_the_key(factory, monkeypatch):
    a = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, provider_mode="bedrock")
    b = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, provider_mode="direct")
    assert a is not b
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    c = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0)
    assert c is a


@pytest.mark.parametrize("kwargs", [{"streaming": True}, {"callbacks": [object()]}])
def test_per_request_state_is_never_cached(factory, kwargs):
    a = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, **kwargs)
    b = providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, **kwargs)
    assert a is not b
    assert factory.call_count == 2
    assert len(providers._model_cache) == 0


def test_cache_is_bounded(factory):
    for i in range(providers._MODEL_CACHE_MAXSIZE + 5):
        providers.get_cached_chat_model("anthropic/claude-haiku-5.5", temperature=0.0, max_tokens=100 + i)
    assert len(providers._model_cache) == providers._MODEL_CACHE_MAXSIZE
    # The oldest entries were evicted; the newest is still there.
    newest = providers.get_cached_chat_model(
        "anthropic/claude-haiku-5.5", temperature=0.0, max_tokens=100 + providers._MODEL_CACHE_MAXSIZE + 4
    )
    assert newest.kwargs["max_tokens"] == 100 + providers._MODEL_CACHE_MAXSIZE + 4
    assert factory.call_count == providers._MODEL_CACHE_MAXSIZE + 5


def test_dict_options_are_hashed_structurally(factory):
    a = providers.get_cached_chat_model(
        "m", temperature=0.0, additional_model_request_fields={"thinking": {"type": "disabled"}, "top_k": 5}
    )
    b = providers.get_cached_chat_model(
        "m", temperature=0.0, additional_model_request_fields={"top_k": 5, "thinking": {"type": "disabled"}}
    )
    assert a is b


def test_clear_drops_everything(factory):
    providers.get_cached_chat_model("m", temperature=0.0)
    providers.clear_chat_model_cache()
    providers.get_cached_chat_model("m", temperature=0.0)
    assert factory.call_count == 2
