"""A model step that times out is sent again; the run and its tool work survive.

Uses the real agent loop. CI installs only langchain-core, so these run where
the full dependencies are installed and are skipped elsewhere.
"""

import asyncio
import importlib
from pathlib import Path

import pytest


def _is_real(name: str) -> bool:
    # conftest puts a stand-in in sys.modules for anything not installed. A
    # stand-in has no file on disk.
    try:
        module = importlib.import_module(name)
    except ImportError:
        return False
    return isinstance(getattr(module, "__file__", None), str)


needs_agent_loop = pytest.mark.skipif(
    # langgraph itself is a namespace package with no file, so ask for a submodule.
    not (_is_real("langchain.agents") and _is_real("langgraph.graph")),
    reason="needs the real langchain agent loop",
)

AGENT_PY = Path(__file__).resolve().parents[2] / "chat" / "backend" / "agent" / "agent.py"


def _agent(script, tools=()):
    """An agent whose model plays ``script`` one step at a time: a message to
    return or an error to raise. Returns the agent and the list of model calls
    (each entry is how many messages that call was sent)."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    from chat.backend.agent.middleware.step_timeout_retry import retry_timed_out_step

    calls = []

    class _Scripted(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            step = script[len(calls)]
            calls.append(len(messages))
            if isinstance(step, BaseException):
                raise step
            return ChatResult(generations=[ChatGeneration(message=step)])

    agent = create_agent(model=_Scripted(), tools=list(tools), middleware=[retry_timed_out_step(initial_delay=0)])
    return agent, calls


@pytest.fixture
def warnings_logged(monkeypatch):
    """What the middleware logged. Read from its own logger: importing the app
    reconfigures the root logger mid-test, which drops pytest's capture handler."""
    from chat.backend.agent.middleware import step_timeout_retry

    seen = []
    monkeypatch.setattr(step_timeout_retry.logger, "warning", lambda message, *args: seen.append(message % args))
    return seen


def _ask(agent):
    from langchain_core.messages import HumanMessage

    return asyncio.run(agent.ainvoke({"messages": [HumanMessage("why is the service slow?")]}))


@needs_agent_loop
def test_a_step_that_times_out_once_is_sent_again_and_the_run_finishes(warnings_logged):
    from langchain_core.messages import AIMessage

    agent, calls = _agent([TimeoutError(), AIMessage(content="the pool is exhausted")])

    assert _ask(agent)["messages"][-1].content == "the pool is exhausted"
    assert len(calls) == 2
    # Nothing else records that a step stalled.
    assert warnings_logged == ["[Agent] A model step timed out. It is tried 3 times in all before the run fails."]


@needs_agent_loop
def test_tool_work_done_before_the_stalled_step_is_kept():
    """The point of retrying the step and not the run."""
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool

    ran = []

    @tool
    def check_pool(service: str) -> str:
        """Look at the connection pool."""
        ran.append(service)
        return "240 of 200 connections in use"

    agent, calls = _agent(
        [
            AIMessage(content="", tool_calls=[{"name": "check_pool", "args": {"service": "payments-db"}, "id": "call-1"}]),
            TimeoutError(),  # the step after the tool result stalls
            AIMessage(content="the pool is exhausted"),
        ],
        tools=[check_pool],
    )
    result = _ask(agent)

    assert ran == ["payments-db"], "the tool ran again, so the run was restarted"
    assert calls[1] == calls[2], "the retried step was not sent the same conversation"
    assert any(isinstance(m, ToolMessage) and "240 of 200" in m.content for m in result["messages"])
    assert result["messages"][-1].content == "the pool is exhausted"


@needs_agent_loop
def test_a_step_that_keeps_timing_out_fails_after_three_tries():
    agent, calls = _agent([TimeoutError()] * 5)

    with pytest.raises(TimeoutError):
        _ask(agent)
    assert len(calls) == 3


@needs_agent_loop
def test_an_error_that_is_not_a_timeout_is_raised_at_once_and_unchanged(warnings_logged):
    """The stock middleware's default would turn it into a message the agent treats as an answer."""
    from langchain_core.messages import AIMessage

    boom = ValueError("429 quota exhausted")
    agent, calls = _agent([boom, AIMessage(content="never reached")])

    with pytest.raises(ValueError) as caught:
        _ask(agent)
    assert caught.value is boom
    assert len(calls) == 1
    assert warnings_logged == []


def test_the_agent_installs_it_next_to_the_context_trim():
    """agentic_tool_flow is too wide to drive here; pin the wiring from source."""
    source = AGENT_PY.read_text(encoding="utf-8")
    assert "middlewares = [ContextTrimMiddleware(model_name=model_name), retry_timed_out_step()]" in source
