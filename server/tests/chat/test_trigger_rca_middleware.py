"""Regression: trigger_rca is enforced by ForceToolChoice middleware, not by a HumanMessage prefix."""

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
        "_force_tool_under_test_trigger_rca", _FORCE_TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_trigger_rca_forced_via_middleware_anthropic(force_tool_module):
    """The middleware is the single source of truth for forcing trigger_rca."""
    request = SimpleNamespace(model=None, tool_choice=None)

    force_tool_module.ForceToolChoice("trigger_rca", provider="anthropic")._patch(request)

    assert request.tool_choice == {"type": "tool", "name": "trigger_rca"}
