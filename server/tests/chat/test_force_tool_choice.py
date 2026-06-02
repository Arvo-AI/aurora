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
