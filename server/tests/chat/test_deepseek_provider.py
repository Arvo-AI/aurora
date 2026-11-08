"""Exercise the real SDK against an in-memory DeepSeek Messages endpoint.

No API key, network service, or production infrastructure is used. These
transport tests need the optional SDKs; the catalog/picker tests run without them.
"""

import asyncio
import json
from importlib.metadata import PackageNotFoundError, version

import pytest

for package in ("langchain-anthropic", "anthropic", "httpx"):
    try:
        version(package)
    except PackageNotFoundError:
        pytest.skip(f"{package} is required for SDK transport tests", allow_module_level=True)

import anthropic
if int(version("anthropic").split(".")[0]) >= 2:
    import httpx2 as httpx
else:
    import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from chat.backend.agent.providers import ProviderRegistry
from chat.backend.agent.providers.deepseek_provider import DeepSeekProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    return DeepSeekProvider()


def _message(content, stop="end_turn"):
    return {
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "deepseek-flash", "content": content,
        "stop_reason": stop, "stop_sequence": None,
        "usage": {
            "input_tokens": 40, "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 0, "output_tokens": 10,
        },
    }


def _tool(name, index):
    return [
        {"type": "thinking", "thinking": f"Check evidence {index}", "signature": "test"},
        {"type": "tool_use", "id": f"call_{index}", "name": name, "input": {"service": "orders"}},
    ]


def _attach_client(model, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    model._client = anthropic.Anthropic(
        api_key="test-deepseek-key", base_url="https://api.deepseek.com/anthropic",
        http_client=client,
    )
    return client


def test_routes_native_and_canonical_ids_to_deepseek(provider):
    registry = ProviderRegistry()
    for model in ("deepseek-flash", "deepseek/deepseek-flash"):
        assert registry.resolve_provider_name(model, "direct") == "deepseek"
        assert registry.resolve_provider_name(model, "deepseek") == "deepseek"
        client = provider.get_chat_model(model)
        assert client.model == "deepseek-flash"
        assert str(client.anthropic_api_url) == "https://api.deepseek.com/anthropic"
    assert not provider.supports_model("deepseek/deepseek-v4-pro")
    with pytest.raises(ValueError, match="not supported"):
        provider.get_chat_model("anthropic/claude-sonnet-5")


def test_missing_key_fails_without_routing_elsewhere(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        ProviderRegistry().get_provider_for_model("deepseek/deepseek-flash", "deepseek")
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider().get_chat_model("deepseek-flash")


def test_two_tool_rounds_preserve_thinking_and_results(provider):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
        assert (
            request.headers.get("x-api-key") == "test-deepseek-key"
            or request.headers.get("authorization") == "Bearer test-deepseek-key"
        )
        assert body["model"] == "deepseek-flash"
        assert body["thinking"]["type"] == "enabled"
        n = len(requests)
        if n > 1:
            assistant, result = body["messages"][-2:]
            assert assistant["content"][0]["type"] == "thinking"
            assert result["content"][0]["tool_use_id"] == f"call_{n - 1}"
        content = _tool("check_service", n) if n < 3 else [{"type": "text", "text": "Deployment caused the error."}]
        return httpx.Response(200, json=_message(content, "tool_use" if n < 3 else "end_turn"))

    model = provider.get_chat_model("deepseek/deepseek-flash")
    tool = {"name": "check_service", "description": "Read service evidence", "input_schema": {
        "type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"],
    }}
    with _attach_client(model, handler):
        bound = model.bind_tools([tool])
        history = [HumanMessage(content="Investigate orders")]
        for index in (1, 2):
            response = bound.invoke(history)
            assert response.tool_calls[0]["args"] == {"service": "orders"}
            assert response.usage_metadata["input_tokens"] == 100
            history.extend([response, ToolMessage(content=f"evidence {index}", tool_call_id=f"call_{index}")])
        final = bound.invoke(history)
    assert final.content == "Deployment caused the error."
    assert len(requests) == 3


class Verdict(BaseModel):
    """Structured incident verdict."""

    cause: str


def test_structured_output_disables_thinking_for_named_tool(provider):
    def handler(request):
        body = json.loads(request.content)
        # DeepSeek rejects a named tool_choice while thinking is enabled.
        assert body["thinking"]["type"] == "disabled"
        assert body["tool_choice"] == {"type": "tool", "name": "Verdict"}
        return httpx.Response(200, json=_message([
            {"type": "tool_use", "id": "verdict", "name": "Verdict", "input": {"cause": "bad deployment"}},
        ], "tool_use"))

    model = provider.get_chat_model("deepseek/deepseek-flash")
    with _attach_client(model, handler):
        result = model.with_structured_output(Verdict, include_raw=True).invoke("Summarize evidence")
    assert result["parsed"].cause == "bad deployment"
    assert result["parsing_error"] is None
    assert model.thinking == {"type": "enabled"}  # Only the structured call changes.


def test_async_agent_executes_tools_and_returns_findings(provider):
    from langchain.agents import create_agent

    requests = []
    executions = []

    def check_service(service: str) -> str:
        """Read diagnostic evidence for a service."""
        executions.append(service)
        return "Deployment changed database hostname."

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=_message(_tool("check_service", 1), "tool_use"))
        assistant, result = body["messages"][-2:]
        assert assistant["content"][0]["thinking"] == "Check evidence 1"
        assert result["content"][0]["tool_use_id"] == "call_1"
        return httpx.Response(200, json=_message([{"type": "text", "text": "Incorrect database hostname."}]))

    async def run():
        model = provider.get_chat_model("deepseek/deepseek-flash")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model._async_client = anthropic.AsyncAnthropic(
                api_key="test-deepseek-key", base_url="https://api.deepseek.com/anthropic", http_client=client,
            )
            agent = create_agent(model=model, tools=[check_service], system_prompt="Investigate using evidence.")
            return await agent.ainvoke({"messages": [HumanMessage(content="Investigate orders")]})

    result = asyncio.run(run())
    assert result["messages"][-1].content == "Incorrect database hostname."
    assert executions == ["orders"]
    assert len(requests) == 2


def _stream_events():
    start = _message([], None)
    start["usage"] = {"input_tokens": 7, "output_tokens": 0, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "test"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Healthy"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"input_tokens": 40, "output_tokens": 10, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 0}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stream_preserves_thinking_and_uses_final_cumulative_usage(provider, asynchronous):
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=_stream_events(), headers={"content-type": "text/event-stream"})

    model = provider.get_chat_model("deepseek/deepseek-flash")
    if asynchronous:
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                model._async_client = anthropic.AsyncAnthropic(
                    api_key="test-deepseek-key", base_url="https://api.deepseek.com/anthropic", http_client=client,
                )
                return [chunk async for chunk in model.astream("Check orders")]
        chunks = asyncio.run(run())
    else:
        with _attach_client(model, handler):
            chunks = list(model.stream("Check orders"))
    combined = chunks[0]
    for chunk in chunks[1:]:
        combined += chunk
    assert any(b.get("thinking") == "Checking" for b in combined.content)
    assert any(b.get("text") == "Healthy" for b in combined.content)
    assert combined.usage_metadata["input_tokens"] == 100
    assert combined.usage_metadata["output_tokens"] == 10
    assert combined.usage_metadata["input_token_details"]["cache_read"] == 60
