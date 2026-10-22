"""Provider-driven prompt-cache markers.

Native Bedrock (ChatBedrockConverse) silently drops ``cache_control``; it needs a
``cachePoint`` block. These tests pin the wire shape with the real langchain-aws
converter, plus the retry-without-markers fallback.
"""
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from chat.backend.agent.providers.anthropic_provider import AnthropicProvider
from chat.backend.agent.providers.base_provider import BaseLLMProvider
from chat.backend.agent.providers.bedrock_provider import BedrockProvider
from chat.backend.agent.providers.openrouter_provider import OpenRouterProvider
from chat.backend.agent.utils.cache_control import (
    BEDROCK_CACHE_POINT,
    CACHE_STYLE_CACHE_CONTROL,
    CACHE_STYLE_BEDROCK,
    build_cached_system_prompt,
    claude_family,
    min_cacheable_chars,
    strip_cache_markers,
    strip_bedrock_tool_cache_points,
    with_bedrock_last_message_cache_point,
    with_bedrock_tools_cache_point,
)

# Above the largest per-model minimum, so cacheable on every model
BIG = "x" * (min_cacheable_chars("unknown-model") + 1)


def _bedrock(base_url=None, region="us-east-1"):
    p = BedrockProvider.__new__(BedrockProvider)
    p.base_url = base_url
    p.region = region
    return p


class _NoStyle(BaseLLMProvider):
    def get_chat_model(self, model, temperature=0.4, **kw):
        raise NotImplementedError

    def is_available(self): return True
    def supports_model(self, model): return True
    def get_native_model_name(self, model): return model


class TestNativeClientBudget:
    def test_native_model_bounds_timeout_and_drops_openai_timeout(self, monkeypatch):
        pytest.importorskip("langchain_aws.chat_models.bedrock_converse")
        monkeypatch.delenv("BEDROCK_BASE_URL", raising=False)
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")

        model = BedrockProvider().get_chat_model(
            "anthropic/claude-sonnet-5", request_timeout=25
        )

        assert model.timeout == 120 and model.max_retries == 3
        cfg = model._get_effective_config()
        assert (cfg.connect_timeout, cfg.read_timeout) == (120, 120)
        assert cfg.retries["max_attempts"] == 3


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
        assert p.prompt_cache_style("anthropic/claude-sonnet-5") == CACHE_STYLE_BEDROCK
        assert p.prompt_cache_style("bedrock/us.anthropic.claude-sonnet-4-5-v1:0") == CACHE_STYLE_BEDROCK

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


def _anthropic():
    return AnthropicProvider.__new__(AnthropicProvider)


class TestClaudeFamily:
    @pytest.mark.parametrize(
        "model_id,family",
        [
            ("anthropic/claude-sonnet-4.5", "claude-sonnet-4-5"),
            ("bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
            ("bedrock/global.anthropic.claude-opus-5-5", "claude-opus-5-5"),
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
            ("openai/gpt-5.5", "openai/gpt-5-5"),
        ],
    )
    def test_normalizes_every_spelling(self, model_id, family):
        assert claude_family(model_id) == family

    def test_minimum_is_per_model_with_safe_default(self):
        assert min_cacheable_chars("anthropic/claude-opus-5.5") == 512 * 4
        assert min_cacheable_chars("bedrock/eu.anthropic.claude-sonnet-5") == 1024 * 4
        assert min_cacheable_chars("anthropic/claude-haiku-4.5") == 4096 * 4
        # sonnet-5-5 must not fall into the shorter sonnet-5 prefix
        assert min_cacheable_chars("anthropic/claude-sonnet-5.5") == 512 * 4
        assert min_cacheable_chars("unknown-model") == 4096 * 4


class TestBuildCachedSystemPrompt:
    def test_no_provider_is_noop(self):
        assert build_cached_system_prompt("anthropic/claude-x", BIG) == BIG

    def test_provider_without_style_is_noop(self):
        assert build_cached_system_prompt("openai/gpt-5.5", BIG, provider=_NoStyle()) == BIG

    def test_small_prompt_is_noop(self):
        assert build_cached_system_prompt("m", "short", provider=_bedrock()) == "short"

    def test_cache_control_shape(self):
        msg = build_cached_system_prompt("anthropic/claude-x", BIG, provider=_anthropic())
        assert isinstance(msg, SystemMessage)
        assert msg.content == [
            {"type": "text", "text": BIG, "cache_control": {"type": "ephemeral"}}
        ]

    def test_cache_point_shape(self):
        msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, provider=_bedrock())
        assert msg.content == [
            {"type": "text", "text": BIG},
            {"cachePoint": {"type": "default"}},
        ]

    def test_style_resolution_error_falls_back(self):
        bad = MagicMock()
        bad.prompt_cache_style.side_effect = RuntimeError("boom")
        assert build_cached_system_prompt("m", BIG, provider=bad) == BIG

    def test_cache_point_goes_between_static_and_dynamic(self):
        msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, "per-session", provider=_bedrock())
        assert msg.content == [
            {"type": "text", "text": BIG},
            {"cachePoint": {"type": "default"}},
            {"type": "text", "text": "per-session"},
        ]

    def test_cache_control_marks_static_and_dynamic(self):
        msg = build_cached_system_prompt("anthropic/claude-x", BIG, "per-session", provider=_anthropic())
        assert [b["text"] for b in msg.content] == [BIG, "per-session"]
        assert all(b["cache_control"] == {"type": "ephemeral"} for b in msg.content)

    def test_rca_sized_static_prefix_is_cached_on_newer_models(self):
        # ~12K chars is the background RCA static prefix; a flat 4096-token gate missed it
        static = "s" * 12_000
        msg = build_cached_system_prompt("anthropic/claude-sonnet-5", static, "alert data", provider=_bedrock())
        assert msg.content[1] == BEDROCK_CACHE_POINT

    def test_short_static_falls_back_to_one_breakpoint_on_the_whole_prompt(self):
        # Haiku 4.5 needs 4096 tokens: the static part alone can't cache, the whole can
        static, dynamic = "s" * 12_000, "d" * 6_000
        msg = build_cached_system_prompt("anthropic/claude-haiku-4.5", static, dynamic, provider=_bedrock())
        assert msg.content == [
            {"type": "text", "text": f"{static}\n{dynamic}"},
            {"cachePoint": {"type": "default"}},
        ]

    def test_whole_prompt_below_minimum_is_plain(self):
        static, dynamic = "s" * 8_000, "d" * 1_000
        out = build_cached_system_prompt("anthropic/claude-haiku-4.5", static, dynamic, provider=_bedrock())
        assert out == f"{static}\n{dynamic}"


class TestRequestCachePoints:
    def test_tools_get_one_trailing_point(self):
        tools = [{"toolSpec": {"name": "a"}}]
        marked = with_bedrock_tools_cache_point(tools)
        assert marked == [*tools, BEDROCK_CACHE_POINT]
        assert tools == [{"toolSpec": {"name": "a"}}]
        # Idempotent: a second pass never spends a fifth point
        assert with_bedrock_tools_cache_point(marked) == marked
        assert with_bedrock_tools_cache_point([]) == []

    def test_last_message_marked_on_a_copy(self):
        history = [HumanMessage("q"), AIMessage("a"), HumanMessage("follow-up")]
        marked = with_bedrock_last_message_cache_point(history)
        assert marked[-1].content == [{"type": "text", "text": "follow-up"}, BEDROCK_CACHE_POINT]
        assert marked[:-1] == history[:-1]
        # Graph state keeps the unmarked message
        assert history[-1].content == "follow-up"
        assert with_bedrock_last_message_cache_point(marked) == marked

    def test_blank_last_message_left_alone(self):
        history = [HumanMessage("   ")]
        assert with_bedrock_last_message_cache_point(history) == history

    def test_strip_bedrock_tool_cache_points(self):
        kwargs = {"tools": [{"toolSpec": {"name": "a"}}, BEDROCK_CACHE_POINT], "stop": None}
        assert strip_bedrock_tool_cache_points(kwargs) == {"tools": [{"toolSpec": {"name": "a"}}], "stop": None}
        assert BEDROCK_CACHE_POINT in kwargs["tools"]
        cfg = {"toolConfig": {"tools": [BEDROCK_CACHE_POINT], "toolChoice": {"auto": {}}}}
        assert strip_bedrock_tool_cache_points(cfg)["toolConfig"] == {"tools": [], "toolChoice": {"auto": {}}}
        assert strip_bedrock_tool_cache_points({"stop": None}) == {"stop": None}


class TestMiddleware:
    def test_marks_tools_and_last_message(self):
        from langchain.agents import middleware as lc_middleware

        # CI stubs langchain with MagicMock, which would make the class itself a mock
        if not isinstance(lc_middleware.AgentMiddleware, type):
            pytest.skip("langchain is stubbed in this environment")
        from chat.backend.agent.middleware import BedrockPromptCacheMiddleware

        seen = {}
        request = MagicMock()
        request.tools = [{"toolSpec": {"name": "a"}}]
        request.messages = [HumanMessage("hi")]
        request.override = lambda **kw: seen.update(kw) or "patched"

        assert BedrockPromptCacheMiddleware().wrap_model_call(request, lambda r: r) == "patched"
        assert seen["tools"][-1] == BEDROCK_CACHE_POINT
        assert seen["messages"][-1].content[-1] == BEDROCK_CACHE_POINT


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
        sys_msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, provider=_bedrock())
        _, system = converse._messages_to_bedrock([sys_msg, HumanMessage("hi")])
        assert system == [{"text": BIG}, {"cachePoint": {"type": "default"}}]

    def test_cache_control_is_dropped_by_converse(self, converse):
        # Why native Bedrock needs cachePoint: Converse discards the Anthropic flag
        sys_msg = build_cached_system_prompt("anthropic/claude-x", BIG, provider=_anthropic())
        _, system = converse._messages_to_bedrock([sys_msg, HumanMessage("hi")])
        assert system == [{"text": BIG}]

    def test_split_system_reaches_wire(self, converse):
        sys_msg = build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, "dyn", provider=_bedrock())
        _, system = converse._messages_to_bedrock([sys_msg, HumanMessage("hi")])
        assert system == [{"text": BIG}, BEDROCK_CACHE_POINT, {"text": "dyn"}]

    def test_last_tool_result_point_lands_after_the_tool_result(self, converse):
        history = with_bedrock_last_message_cache_point([
            HumanMessage("q"),
            AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "call-1"}]),
            ToolMessage("result", tool_call_id="call-1"),
        ])
        messages, _ = converse._messages_to_bedrock(history)
        last = messages[-1]["content"]
        assert "toolResult" in last[0] and last[-1] == BEDROCK_CACHE_POINT
        assert BEDROCK_CACHE_POINT not in last[0]["toolResult"]["content"]

    def test_tools_point_reaches_tool_config(self, converse):
        tools = with_bedrock_tools_cache_point([{"toolSpec": {"name": "t", "inputSchema": {"json": {}}}}])
        assert converse._format_tools(tools)[-1] == BEDROCK_CACHE_POINT


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
        # CI stubs the top-level langchain_aws package, so probe a real submodule
        pytest.importorskip("langchain_aws.chat_models.bedrock_converse")
        from langchain_aws import ChatBedrockConverse

        from chat.backend.agent.providers._bedrock_cache_guard import make_bedrock_cache_safe_cls

        return make_bedrock_cache_safe_cls(ChatBedrockConverse)

    def _model(self, model_cls, client):
        return model_cls(
            model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            client=client,
            region_name="us-east-1",
        )

    def _messages(self):
        return [build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, provider=_bedrock()), HumanMessage("hi")]

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

    def test_bound_tools_point_reaches_converse_and_is_stripped_on_rejection(self, model_cls):
        client = MagicMock()
        client.converse.side_effect = [
            Exception("ValidationException: cachePoint is not supported for this model"),
            _converse_response(),
        ]
        tool = {"toolSpec": {"name": "t", "description": "d", "inputSchema": {"json": {"type": "object"}}}}
        bound = self._model(model_cls, client).bind_tools(with_bedrock_tools_cache_point([tool]))

        assert bound.invoke(self._messages()).content == "ok"
        first, retry = (c.kwargs["toolConfig"]["tools"] for c in client.converse.call_args_list)
        assert first[-1] == BEDROCK_CACHE_POINT
        assert BEDROCK_CACHE_POINT not in retry and len(retry) == 1

    def test_agent_request_carries_three_points(self, model_cls):
        # Whole path: create_agent -> middleware -> ChatBedrockConverse -> Converse call
        from langchain.agents import create_agent
        from langchain_core.tools import tool

        from chat.backend.agent.middleware import BedrockPromptCacheMiddleware

        @tool
        def lookup(q: str) -> str:
            """Look something up."""
            return "found"

        client = MagicMock()
        client.converse.return_value = _converse_response()
        agent = create_agent(
            model=self._model(model_cls, client),
            tools=[lookup],
            system_prompt=build_cached_system_prompt("anthropic/claude-sonnet-5", BIG, "dyn", provider=_bedrock()),
            middleware=[BedrockPromptCacheMiddleware()],
        )
        result = agent.invoke({"messages": [HumanMessage("hi")]})

        call = client.converse.call_args.kwargs
        points = (
            call["system"].count(BEDROCK_CACHE_POINT)
            + call["toolConfig"]["tools"].count(BEDROCK_CACHE_POINT)
            + sum(m["content"].count(BEDROCK_CACHE_POINT) for m in call["messages"])
        )
        assert points == 3
        assert call["toolConfig"]["tools"][-1] == BEDROCK_CACHE_POINT
        assert call["messages"][-1]["content"][-1] == BEDROCK_CACHE_POINT
        # The marker is request-only; graph state keeps the plain message
        assert result["messages"][0].content == "hi"

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

        # langchain-aws >= 1.4.6 already folds cache tokens into input_tokens, and the
        # normalizer must not add them a second time.
        assert msg.usage_metadata["input_tokens"] == 1000
        norm = normalize_usage(msg.usage_metadata)
        assert (norm.input_tokens, norm.cache_read_tokens, norm.cache_write_tokens) == (1000, 800, 100)


class _FakeChatModel(BaseModel):
    """Stand-in for a chat client: records the messages each attempt received."""

    attempts: list = Field(default_factory=list)
    attempt_kwargs: list = Field(default_factory=list)
    errors: list = Field(default_factory=list)  # popped front-first, one per attempt

    def _generate(self, messages, *args, **kwargs):
        self.attempts.append(messages)
        self.attempt_kwargs.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return "ok"

    def _stream(self, messages, *args, **kwargs):
        self.attempts.append(messages)
        if self.errors:
            raise self.errors.pop(0)
        yield "chunk"


def _marked_messages():
    system = SystemMessage(content=[{"type": "text", "text": "sys"}, {"cachePoint": {"type": "default"}}])
    return [system, HumanMessage("hi")]


def _has_marker(messages):
    return any(isinstance(b, dict) and "cachePoint" in b for m in messages if isinstance(m.content, list) for b in m.content)


class TestCacheGuardLogic:
    """Retry behavior with a fake client, so it runs without langchain-aws."""

    @pytest.fixture()
    def model(self):
        from chat.backend.agent.providers._bedrock_cache_guard import make_bedrock_cache_safe_cls

        return make_bedrock_cache_safe_cls(_FakeChatModel)()

    def test_cache_rejection_retries_clean_then_stays_clean(self, model):
        model.errors.append(Exception("ValidationException: cachePoint not supported"))
        assert model._generate(_marked_messages()) == "ok"
        assert _has_marker(model.attempts[0]) and not _has_marker(model.attempts[1])

        # Later calls skip the doomed first attempt entirely
        model._generate(_marked_messages())
        assert len(model.attempts) == 3 and not _has_marker(model.attempts[2])

    def test_rejection_also_strips_the_tools_point(self, model):
        model.errors.append(Exception("ValidationException: cachePoint not supported"))
        tools = [{"toolSpec": {"name": "a"}}, BEDROCK_CACHE_POINT]
        model._generate(_marked_messages(), tools=tools)
        assert model.attempt_kwargs[0]["tools"] == tools
        assert model.attempt_kwargs[1]["tools"] == [{"toolSpec": {"name": "a"}}]
        model._generate(_marked_messages(), tools=tools)
        assert model.attempt_kwargs[2]["tools"] == [{"toolSpec": {"name": "a"}}]

    def test_unrelated_error_propagates_without_retry(self, model):
        model.errors.append(Exception("ThrottlingException: slow down"))
        with pytest.raises(Exception, match="Throttling"):
            model._generate(_marked_messages())
        assert len(model.attempts) == 1

    def test_second_cache_error_is_not_retried_again(self, model):
        model.errors.extend([Exception("cachePoint bad"), Exception("cachePoint still bad")])
        with pytest.raises(Exception, match="still bad"):
            model._generate(_marked_messages())
        assert len(model.attempts) == 2

    def test_stream_rejection_retries_clean(self, model):
        model.errors.append(Exception("prompt caching is not supported"))
        assert list(model._stream(_marked_messages())) == ["chunk"]
        assert _has_marker(model.attempts[0]) and not _has_marker(model.attempts[1])

    def test_stream_error_after_output_is_not_retried(self, model):
        def boom_midstream(messages, *a, **k):
            yield "partial"
            raise Exception("cachePoint rejected late")

        # Patch the base behavior: error surfaces only after a chunk was yielded
        base = type(model).__mro__[1]
        original = base._stream
        base._stream = boom_midstream
        try:
            gen = model._stream(_marked_messages())
            assert next(gen) == "partial"
            with pytest.raises(Exception, match="rejected late"):
                next(gen)
        finally:
            base._stream = original
