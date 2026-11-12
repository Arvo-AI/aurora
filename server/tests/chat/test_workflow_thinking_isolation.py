"""Exercise real workflow streaming/consolidation without LLM, DB or Slack I/O."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from chat.backend.agent.utils.message_content import extract_text_from_content

THOUGHT = "Thinking Process: Summarize the provided thoughts in the first person."
ANSWER = "The 400 MB cache exceeds the 256 MiB limit. Reduce the cache."
BLOCKS = [
    {"type": "thinking", "thinking": THOUGHT, "signature": "keep-signature"},
    {"type": "text", "text": ANSWER},
]


@pytest.fixture
def workflow(monkeypatch):
    # Stub only graph setup and I/O dependencies, not the methods under test.
    for name, attr in [
        ("chat.backend.agent.agent", "Agent"),
        ("chat.backend.agent.utils.safe_memory_saver", "SafeMemorySaver"),
        ("chat.backend.agent.utils.llm_context_manager", "LLMContextManager"),
        ("chat.backend.agent.utils.chat_context_manager", "ChatContextManager"),
        ("chat.backend.agent.utils.tool_context_capture", "ToolContextCapture"),
        ("guardrails.input_rail", "check_input"),
    ]:
        module = ModuleType(name)
        setattr(module, attr, MagicMock())
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "chat/backend/agent/workflow.py"
    spec = importlib.util.spec_from_file_location("_thinking_test_workflow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Workflow, "get_compiled_workflow", lambda self: MagicMock())
    return module.Workflow(MagicMock(), "test-session")


def run_stream(workflow, chunks, output, saved, *, background=True, interrupt=False):
    async def events(*args, **kwargs):
        yield {"event": "on_chat_model_start"}
        for chunk in chunks:
            yield {"event": "on_chat_model_stream", "data": {"chunk": chunk}}
        if interrupt:
            workflow._last_state = {"messages": [saved]}
            raise asyncio.CancelledError()
        if output is not None:
            yield {"event": "on_chat_model_end", "data": {"output": output}}
        yield {"event": "on_chain_end", "name": "LangGraph", "data": {"output": {"messages": [saved]}}}

    workflow.app.astream_events = events
    state = SimpleNamespace(messages=[], session_id=None, user_id=None, model="test", is_background=background)
    progress = []

    async def consume():
        try:
            async for kind, value in workflow.stream(state):
                if kind == "token":
                    progress.append(value)
        except asyncio.CancelledError:
            if not interrupt:
                raise
            workflow._consolidate_message_chunks()

    asyncio.run(consume())
    ui = workflow._convert_to_ui_messages(workflow._last_state["messages"])
    return "".join(progress), [msg["text"] for msg in ui if msg["sender"] == "bot"]


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("background", [True, False])
def test_thoughts_never_replace_complete_answer(workflow, streaming, background):
    output = AIMessage(id="reply", content=BLOCKS)
    chunks = [AIMessageChunk(id="reply", content=BLOCKS)] if streaming else []
    progress, answers = run_stream(workflow, chunks, output, output, background=background)
    assert answers == [ANSWER]
    assert (THOUGHT in progress) is background
    assert output.content == BLOCKS  # retain signatures in model context


@pytest.mark.parametrize("kind", ["thinking", "reasoning", "reasoning_content"])
def test_cancellation_recovers_answer_only(workflow, kind):
    if kind == "reasoning_content":
        thought = AIMessageChunk(id="reply", content="", additional_kwargs={kind: THOUGHT})
    elif kind == "reasoning":
        thought = AIMessageChunk(id="reply", content=[{"type": kind, "summary": [{"text": THOUGHT}]}])
    else:
        thought = AIMessageChunk(id="reply", content=[BLOCKS[0]])
    chunks = [thought, AIMessageChunk(id="reply", content=ANSWER)]
    saved = AIMessage(id="reply", content=[BLOCKS[0]])
    progress, answers = run_stream(workflow, chunks, None, saved, interrupt=True)
    assert answers == [ANSWER]
    assert THOUGHT in progress
    assert saved.content[0] == BLOCKS[0]


def test_thinking_only_response_is_not_an_answer(workflow):
    output = AIMessage(id="reply", content=[BLOCKS[0]])
    progress, answers = run_stream(workflow, [], output, output)
    assert progress == THOUGHT
    assert answers == [""]


def test_model_end_recovers_answer_when_only_thinking_streamed(workflow):
    saved = AIMessage(id="reply", content=[BLOCKS[0]])
    _, answers = run_stream(workflow, [AIMessageChunk(id="reply", content=[BLOCKS[0]])],
                            AIMessage(id="reply", content=BLOCKS), saved)
    assert answers == [ANSWER]


def test_tool_call_signature_survives_recovery(workflow):
    tool = {"name": "inspect_pod", "args": {}, "id": "call_test", "type": "tool_call"}
    signature = {"__gemini_function_call_thought_signatures__": {"call_test": "opaque-signature"}}
    saved = AIMessage(id="reply", content="", tool_calls=[tool], additional_kwargs=signature)
    _, answers = run_stream(workflow, [AIMessageChunk(id="reply", content=BLOCKS)], None, saved, interrupt=True)
    assert answers == [ANSWER]
    assert saved.tool_calls == [tool]
    assert saved.additional_kwargs == signature


def test_plain_string_reply_and_per_turn_reset(workflow):
    output = AIMessage(id="reply", content=ANSWER)
    assert run_stream(workflow, [AIMessageChunk(id="reply", content=ANSWER)], output, output)[1] == [ANSWER]
    # Reusing an ID in the next turn must not recover an old answer.
    thought_only = AIMessage(id="reply", content=[BLOCKS[0]])
    assert run_stream(workflow, [], thought_only, thought_only)[1] == [""]
    assert extract_text_from_content(thought_only.content) == ""
