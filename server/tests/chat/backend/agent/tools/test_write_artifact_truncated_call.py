"""A write_artifact call that arrives without ``content`` must reach the tool
and come back with an actionable error.

This is what a tool call cut off by the model's output-token limit looks like:
the arguments JSON ends after ``title`` (or before any argument), so the parsed
args are ``{"title": ...}`` or ``{}``. With the pydantic args schema the
ToolNode rejected the call before the tool function (and its execution-step
capture wrapper) ran, the model saw only "content: Field required", retried the
same oversized document, and each attempt left a step stuck in 'running'.

The calls below go through a real ``create_agent`` graph and ToolNode, with a
scripted model standing in for the LLM.
"""

import functools
import json

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_aws.function_calling import convert_to_anthropic_tool

from chat.backend.agent.tools import artifact_tool
from chat.backend.agent.tools.artifact_tool import WRITE_ARTIFACT_TOOL_SCHEMA, write_artifact


class _ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _tool(func):
    # Same construction as cloud_tools.get_cloud_tools for write_artifact.
    return StructuredTool.from_function(
        func=func, name="write_artifact", description="write an artifact",
        args_schema=WRITE_ARTIFACT_TOOL_SCHEMA,
    )


def _run_one_call(tool, args):
    model = _ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "write_artifact", "args": args}]),
        AIMessage(content="done"),
    ])
    out = create_agent(model=model, tools=[tool]).invoke({"messages": [("user", "go")]})
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


def test_schema_shown_to_model_still_requires_title_and_content():
    schema = convert_to_anthropic_tool(_tool(write_artifact))["input_schema"]
    assert set(schema["required"]) == {"title", "content"}


@pytest.mark.parametrize("args", [{"title": "Incident Index"}, {}])
def test_truncated_call_reaches_tool_and_explains_truncation(args, monkeypatch):
    invoked = []

    def capture(**kwargs):
        invoked.append(kwargs)
        return write_artifact(user_id="user-1", **kwargs)

    # Nothing may be written for a call without content.
    monkeypatch.setattr(
        "utils.db.connection_pool.db_pool.get_admin_connection",
        lambda *a, **k: pytest.fail("write_artifact touched the database"),
    )

    msgs = _run_one_call(_tool(capture), args)

    assert invoked == [args], "tool function (and its capture wrapper) must run"
    assert len(msgs) == 1 and msgs[0].tool_call_id == "call-1"
    body = json.loads(msgs[0].content)
    assert body == {"error": artifact_tool._MISSING_CONTENT_ERROR}
    assert "output-token limit" in body["error"]


def test_empty_string_content_keeps_existing_error():
    assert json.loads(write_artifact(title="t", content="  ", user_id="u")) == {
        "error": "content cannot be empty."
    }
