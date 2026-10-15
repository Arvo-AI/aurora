"""Provider-driven prompt-cache markers.

Native Bedrock (ChatBedrockConverse) silently drops ``cache_control``; it needs a
``cachePoint`` block. These tests pin the wire shape with the real langchain-aws
converter, plus the retry-without-markers fallback.
"""
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from chat.backend.agent.providers.anthropic_provider import AnthropicProvider
from chat.backend.agent.providers.base_provider import BaseLLMProvider
from chat.backend.agent.providers.bedrock_provider import BedrockProvider
from chat.backend.agent.providers.openrouter_provider import OpenRouterProvider
from chat.backend.agent.utils import cache_control as cc
from chat.backend.agent.utils.cache_control import (
    CACHE_STYLE_CACHE_CONTROL,
    CACHE_STYLE_CACHE_POINT,
    build_cached_system_prompt,
    strip_cache_markers,
)

BIG = "x" * (cc._MIN_PREFIX_CHARS + 1)


def _bedrock(base_url=None, region="us-east-1"):
    p = BedrockProvider.__new__(BedrockProvider)
    p.base_url = base_url
    p.region = region
    return p


class _NoStyle(BaseLLMProvider):
    def get_chat_model(self, model, temperature=0.4, **kw): ...
    def is_available(self): return True
    def supports_model(self, model): return True
    def get_native_model_name(self, model): return model


class TestProviderStyles:
    def test_anthropic_direct_uses_cache_control(self):
        p = AnthropicProvider.__new__(AnthropicProvider)
        assert p.prompt_cache_style("anthropic/claude-opus-5-5") == CACHE_STYLE_CACHE_CONTROL

    def test_openrouter_tags_claude_only(self):
        p = OpenRouterProvider.__new__(OpenRouterProvider)
        assert p.prompt_cache_style("anthropic/claude-sonnet-5") == CACHE_STYLE_CACHE_CONTROL
        assert p.prompt_cache_style("openai/gpt-5.5") is None
        assert p.prompt_cache_style("google/gemini-3.5-flash") is None

    def test_bedrock_native_uses_cache_point(self):
        p = _bedrock()
        assert p.prompt_cache_style("anthropic/claude-sonnet-5") == CACHE_STYLE_CACHE_POINT
        assert p.prompt_cache_style("bedrock/us.anthropic.claude-sonnet-4-5-v1:0") == CACHE_STYLE_CACHE_POINT

    @pytest.mark.parametrize(
        "model",
        [
            "bedrock/us.anthropic.claude-3-haiku-20240307-v1:0",
            "bedrock/us.anthropic.claude-3-5-sonnet-20240620-v1:0",
            "bedrock/amazon.titan-text-express-v1",
        ],
    )
    def test_bedrock_native_skips_uncacheable_models(self, model):
        assert _bedrock().prompt_cache_style(model) is None

    def test_bedrock_gateway_keeps_legacy_cache_control(self):
        p = _bedrock(base_url="https://gw.example.com/v1")
        assert p.prompt_cache_style("anthropic/claude-sonnet-5") == CACHE_STYLE_CACHE_CONTROL
        assert p.prompt_cache_style("bedrock/us.anthropic.claude-sonnet-4-5-v1:0") is None

    def test_default_provider_has_no_style(self):
        assert _NoStyle().prompt_cache_style("openai/gpt-5.5") is None


class TestBuildCachedSystemPrompt:
    def test_no_provider_is_noop(self):
        assert build_cached_system_prompt("anthropic/claude-x", BIG) == BIG

    def test_provider_without_style_is_noop(self):
        assert build_cached_system_prompt("openai/gpt-5.5", BIG, _NoStyle()) == BIG

    def test_small_prompt_is_noop(self):
        assert build_cached_system_prompt("m", "short", _bedrock()) == "short"

    def test_cache_control_shape(self):
        p = AnthropicProvider.__new__(AnthropicProvider)
        msg = build_cached_system_prompt("anthropic/claude-x", BIG, p)
        assert isinstance(msg, SystemMessage)
        assert msg.content == [
            {"type": "text", "text": BIG, "cache_control": {"type": "ephemeral"}}
        ]

    def test_cache_point_shape(self):
        msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, _bedrock())
        assert msg.content == [
            {"type": "text", "text": BIG},
            {"cachePoint": {"type": "default"}},
        ]

    def test_style_resolution_error_falls_back(self):
        bad = MagicMock()
        bad.prompt_cache_style.side_effect = RuntimeError("boom")
        assert build_cached_system_prompt("m", BIG, bad) == BIG


class TestStripCacheMarkers:
    def test_removes_both_dialects_without_touching_originals(self):
        point = SystemMessage(content=[{"type": "text", "text": "a"}, {"cachePoint": {"type": "default"}}])
        flag = SystemMessage(content=[{"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}])
        out = strip_cache_markers([point, flag, HumanMessage("hi")])
        assert out[0].content == [{"type": "text", "text": "a"}]
        assert out[1].content == [{"type": "text", "text": "b"}]
        assert out[2].content == "hi"
        # originals unchanged so shared state never loses its markers
        assert {"cachePoint": {"type": "default"}} in point.content


class TestBedrockConverseWire:
    """Real langchain-aws conversion: what actually goes to Bedrock."""

    @pytest.fixture()
    def converse(self):
        return pytest.importorskip("langchain_aws.chat_models.bedrock_converse")

    def test_cache_point_reaches_wire(self, converse):
        sys_msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, _bedrock())
        _, system = converse._messages_to_bedrock([sys_msg, HumanMessage("hi")])
        assert system == [{"text": BIG}, {"cachePoint": {"type": "default"}}]

    def test_cache_control_is_dropped_by_converse(self, converse):
        # Why native Bedrock needs cachePoint: Converse discards the Anthropic flag
        p = AnthropicProvider.__new__(AnthropicProvider)
        sys_msg = build_cached_system_prompt("anthropic/claude-x", BIG, p)
        _, system = converse._messages_to_bedrock([sys_msg, HumanMessage("hi")])
        assert system == [{"text": BIG}]


def _converse_response(cache_read=0, cache_write=0):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 5,
            "totalTokens": 105 + cache_read + cache_write,
            "cacheReadInputTokens": cache_read,
            "cacheWriteInputTokens": cache_write,
        },
        "metrics": {"latencyMs": 1},
        "ResponseMetadata": {},
    }


class TestCacheSafeConverse:
    @pytest.fixture()
    def model_cls(self):
        pytest.importorskip("langchain_aws")
        from langchain_aws import ChatBedrockConverse

        from chat.backend.agent.providers._cache_guard import make_cache_safe_cls

        return make_cache_safe_cls(ChatBedrockConverse)

    def _model(self, model_cls, client):
        return model_cls(
            model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            client=client,
            region_name="us-east-1",
        )

    def _messages(self):
        return [build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, _bedrock()), HumanMessage("hi")]

    def test_rejection_retries_without_markers_then_stays_off(self, model_cls):
        client = MagicMock()
        client.converse.side_effect = [
            Exception("ValidationException: cachePoint is not supported for this model"),
            _converse_response(),
            _converse_response(),
        ]
        model = self._model(model_cls, client)

        assert model.invoke(self._messages()).content == "ok"
        first, retry = (c.kwargs["system"] for c in client.converse.call_args_list)
        assert {"cachePoint": {"type": "default"}} in first
        assert retry == [{"text": BIG}]

        # Later calls go out clean without burning another failed request
        model.invoke(self._messages())
        assert client.converse.call_count == 3
        assert client.converse.call_args.kwargs["system"] == [{"text": BIG}]

    def test_unrelated_errors_are_not_swallowed(self, model_cls):
        client = MagicMock()
        client.converse.side_effect = Exception("ThrottlingException: slow down")
        model = self._model(model_cls, client)
        with pytest.raises(Exception, match="Throttling"):
            model.invoke(self._messages())
        assert client.converse.call_count == 1

    def test_usage_reports_cache_tokens_and_normalizes(self, model_cls):
        from chat.backend.agent.utils.usage_normalizer import normalize_usage

        client = MagicMock()
        client.converse.return_value = _converse_response(cache_read=800, cache_write=100)
        msg = self._model(model_cls, client).invoke(self._messages())

        norm = normalize_usage(msg.usage_metadata)
        assert (norm.input_tokens, norm.cache_read_tokens, norm.cache_write_tokens) == (1000, 800, 100)
