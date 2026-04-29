"""Regression coverage for chat-triggered RCA input rail handling."""

import sys
from types import ModuleType


def _stub_module(name, **attrs):
    module = ModuleType(name)
    for attr, value in attrs.items():
        setattr(module, attr, value)
    sys.modules[name] = module
    return module


class _Dummy:
    pass


class _DummyRunnableConfig(dict):
    pass


_stub_module("langgraph")
_stub_module("langgraph.graph", StateGraph=_Dummy, START="START", END="END")
_stub_module("langgraph.graph.state", CompiledStateGraph=_Dummy)
_stub_module("langchain_core")
_stub_module("langchain_core.runnables")
_stub_module("langchain_core.runnables.config", RunnableConfig=_DummyRunnableConfig)
_stub_module(
    "langchain_core.messages",
    AIMessageChunk=_Dummy,
    AIMessage=_Dummy,
    SystemMessage=_Dummy,
    AnyMessage=_Dummy,
)
_stub_module("chat.backend.agent.agent", Agent=_Dummy)
_stub_module(
    "chat.backend.agent.utils.safe_memory_saver",
    SafeMemorySaver=_Dummy,
)
_stub_module("chat.backend.agent.utils.state", State=_Dummy)
_stub_module("utils.auth.stateless_auth", set_rls_context=lambda *args, **kwargs: None)
_stub_module("utils.security.audit_events", emit_block_event=lambda *args, **kwargs: None)

from chat.backend.agent.workflow import _get_input_rail_text


def test_rca_input_rail_uses_original_user_question():
    rca_augmented_message = (
        "[RCA INVESTIGATION REQUESTED]\n"
        "The user has explicitly requested a Root Cause Analysis investigation. "
        "You MUST call the trigger_rca tool with their message as the issue_description. "
        "Extract a short title, affected service, and severity from their description.\n\n"
        "there is high cpu memory"
    )

    assert _get_input_rail_text(
        question="there is high cpu memory",
        message_content=rca_augmented_message,
    ) == "there is high cpu memory"


def test_input_rail_falls_back_to_message_content_without_question():
    assert _get_input_rail_text(
        question=None,
        message_content=[{"type": "text", "text": "check high cpu"}],
    ) == "check high cpu"
