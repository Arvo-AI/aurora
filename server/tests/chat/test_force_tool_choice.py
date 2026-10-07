"""Provider-specific forced tool choice formatting."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest


_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_FORCE_TOOL_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "middleware", "force_tool.py"
)


@pytest.fixture()
def force_tool_module(monkeypatch):
    """Load the middleware with minimal LangChain stubs for focused tests."""
    langchain = types.ModuleType("langchain")
    agents = types.ModuleType("langchain.agents")
    middleware = types.ModuleType("langchain.agents.middleware")
    middleware_types = types.ModuleType("langchain.agents.middleware.types")

    class AgentMiddleware:
        pass

    class ModelRequest:
        pass

    middleware.AgentMiddleware = AgentMiddleware
    middleware_types.ModelRequest = ModelRequest

    monkeypatch.setitem(sys.modules, "langchain", langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", agents)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware", middleware)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware.types", middleware_types)

    spec = importlib.util.spec_from_file_location(
        "_force_tool_under_test", _FORCE_TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request(model=None):
    return SimpleNamespace(model=model, tool_choice=None)


def _model(class_name: str, module: str, **attrs):
    cls = type(class_name, (), {})
    cls.__module__ = module
    instance = cls()
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openrouter", {"type": "function", "function": {"name": "trigger_rca"}}),
        ("openai", {"type": "function", "function": {"name": "trigger_rca"}}),
        ("anthropic", {"type": "tool", "name": "trigger_rca"}),
        ("google", "trigger_rca"),
        ("vertex", "trigger_rca"),
        (None, {"type": "function", "function": {"name": "trigger_rca"}}),
    ],
)
def test_formats_tool_choice_for_transport_provider(force_tool_module, provider, expected):
    request = _request()

    force_tool_module.ForceToolChoice("trigger_rca", provider=provider)._patch(request)

    assert request.tool_choice == expected


def test_infers_openai_shape_from_chat_openai_for_openrouter_model(force_tool_module):
    model = _model(
        "ChatOpenAI",
        "langchain_openai.chat_models.base",
        model_name="anthropic/claude-sonnet-4.5",
    )
    request = _request(model=model)

    force_tool_module.ForceToolChoice("trigger_rca")._patch(request)

    assert request.tool_choice == {
        "type": "function",
        "function": {"name": "trigger_rca"},
    }


def test_infers_google_shape_through_wrapped_model(force_tool_module):
    google_model = _model(
        "ChatGoogleGenerativeAI",
        "langchain_google_genai.chat_models",
    )
    wrapper = SimpleNamespace(bound=google_model)
    request = _request(model=wrapper)

    force_tool_module.ForceToolChoice("trigger_rca")._patch(request)

    assert request.tool_choice == "trigger_rca"


def test_forces_only_first_model_call(force_tool_module):
    middleware = force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")
    first = _request()
    second = _request()

    middleware._patch(first)
    middleware._patch(second)

    assert first.tool_choice == {"type": "tool", "name": "trigger_rca"}
    assert second.tool_choice is None


def test_uses_model_request_override_when_available(force_tool_module):
    class Request(SimpleNamespace):
        def override(self, **kwargs):
            return Request(**{**self.__dict__, **kwargs})

    request = Request(model=None, tool_choice=None)

    patched = force_tool_module.ForceToolChoice("trigger_rca", provider="google")._patch(
        request
    )

    assert patched is not request
    assert patched.tool_choice == "trigger_rca"
    assert request.tool_choice is None


# ---------------------------------------------------------------------------
# Fallback for models that reject a forced tool_choice (Opus 5.5, Fable 5.1)
# ---------------------------------------------------------------------------

_REJECTION = (
    'Error code: 400 - tool_choice: type "tool" and "any" are not supported '
    "for this model."
)


@pytest.fixture(autouse=True)
def _clear_unsupported_cache(force_tool_module):
    """The rejection cache is module-global; isolate it between tests."""
    force_tool_module._FORCE_UNSUPPORTED.clear()
    yield
    force_tool_module._FORCE_UNSUPPORTED.clear()


def _messages_request(model=None):
    return SimpleNamespace(model=model, tool_choice=None, messages=["original"])


def test_falls_back_to_auto_when_model_rejects_forcing(force_tool_module):
    """Opus 5.5 rejects forcing; Trigger RCA must still reach the tool, not 400."""
    calls = []

    def call_next(request):
        calls.append(request.tool_choice)
        # First (forced) attempt is what the provider rejects.
        if request.tool_choice != "auto":
            raise RuntimeError(_REJECTION)
        return "ok"

    request = _messages_request(_model("ChatAnthropic", "langchain_anthropic.chat_models",
                                      model_name="claude-opus-5-5"))
    middleware = force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")

    assert middleware.wrap_model_call(request, call_next) == "ok"
    assert calls == [{"type": "tool", "name": "trigger_rca"}, "auto"]


def test_fallback_appends_prompt_directive_naming_the_tool(force_tool_module):
    """Without a forced tool_choice the instruction has to be in the prompt."""
    seen = {}

    def call_next(request):
        if request.tool_choice != "auto":
            raise RuntimeError(_REJECTION)
        seen["messages"] = request.messages
        return "ok"

    request = _messages_request(_model("ChatAnthropic", "langchain_anthropic.chat_models",
                                      model_name="claude-opus-5-5"))
    force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic").wrap_model_call(
        request, call_next
    )

    assert seen["messages"][0] == "original"
    assert "trigger_rca" in str(seen["messages"][-1].content)


def test_rejection_is_remembered_so_later_runs_skip_the_400(force_tool_module):
    """Second invocation for the same model must not repeat the wasted call."""
    model = _model("ChatAnthropic", "langchain_anthropic.chat_models",
                   model_name="claude-opus-5-5")

    def call_next(request):
        if request.tool_choice != "auto":
            raise RuntimeError(_REJECTION)
        return "ok"

    force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic").wrap_model_call(
        _messages_request(model), call_next
    )

    attempts = []

    def counting_call_next(request):
        attempts.append(request.tool_choice)
        return "ok"

    force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic").wrap_model_call(
        _messages_request(model), counting_call_next
    )

    assert attempts == ["auto"], "forced attempt should be skipped after a known rejection"


def test_unrelated_errors_are_not_swallowed(force_tool_module):
    """Only tool_choice rejections trigger the fallback — everything else propagates."""
    def call_next(request):
        raise RuntimeError("Error code: 429 - rate limit exceeded")

    request = _messages_request()
    middleware = force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")

    with pytest.raises(RuntimeError, match="rate limit"):
        middleware.wrap_model_call(request, call_next)


def test_supported_model_still_gets_a_hard_forced_tool_choice(force_tool_module):
    """No behaviour change for Sonnet 5 and friends: one call, forced."""
    calls = []

    def call_next(request):
        calls.append(request.tool_choice)
        return "ok"

    request = _messages_request(_model("ChatAnthropic", "langchain_anthropic.chat_models",
                                      model_name="claude-sonnet-5"))
    force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic").wrap_model_call(
        request, call_next
    )

    assert calls == [{"type": "tool", "name": "trigger_rca"}]


def test_later_turns_pass_through_untouched(force_tool_module):
    """The middleware forces only the first turn, then steps aside."""
    calls = []

    def call_next(request):
        calls.append(request.tool_choice)
        return "ok"

    middleware = force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")
    middleware.wrap_model_call(_messages_request(), call_next)
    middleware.wrap_model_call(_messages_request(), call_next)

    assert calls == [{"type": "tool", "name": "trigger_rca"}, None]


def test_async_path_falls_back_the_same_way(force_tool_module):
    # asyncio.run rather than pytest-asyncio: the CI test env installs neither
    # pytest-asyncio nor anyio, and pytest.ini runs with --strict-markers.
    import asyncio

    calls = []

    async def call_next(request):
        calls.append(request.tool_choice)
        if request.tool_choice != "auto":
            raise RuntimeError(_REJECTION)
        return "ok"

    request = _messages_request(_model("ChatAnthropic", "langchain_anthropic.chat_models",
                                      model_name="claude-opus-5-5"))
    middleware = force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")

    assert asyncio.run(middleware.awrap_model_call(request, call_next)) == "ok"
    assert calls == [{"type": "tool", "name": "trigger_rca"}, "auto"]
