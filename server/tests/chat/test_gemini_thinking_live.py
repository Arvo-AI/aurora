"""Opt-in Vertex smoke test: real model, synthetic tool, stubbed DB and Slack.

With Vertex credentials configured, run from server/:
    python -m pytest tests/chat/test_gemini_thinking_live.py --run-gemini-live -q -s

This incurs model usage. No cloud command executes and no message reaches Slack.
"""

import pytest

from tests.chat.test_slack_reply_aurora_link import send  # noqa: F401
from tests.chat.test_workflow_thinking_isolation import run_stream, workflow  # noqa: F401

@pytest.fixture(autouse=True)
def _require_live_opt_in(request):
    if not request.config.getoption("--run-gemini-live"):
        pytest.skip("opt-in live Vertex test")


@pytest.mark.parametrize("model,streaming", [
    ("vertex/gemini-4-carbon", False),
    ("vertex/gemini-4-carbon", True),
    ("vertex/gemini-4-argon", True),
])
def test_live_tool_round_trip_and_slack_answer(model, streaming, workflow, send):
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool
    from chat.backend.agent.providers.vertex_provider import VertexAIProvider
    from chat.backend.agent.utils.message_content import extract_text_from_content

    @tool
    def inspect_pod() -> str:
        """Read the synthetic pod's cache size and memory limit."""
        return "Cache increased from 100 MB to 400 MB after deploy; memory limit 256 MiB; OOMKilled."

    llm = VertexAIProvider().get_chat_model(model, timeout=120, max_retries=0)
    assert llm.include_thoughts is False
    messages = [HumanMessage(content="Call inspect_pod, explain the OOM root cause and give two fixes in at most four sentences.")]
    call = llm.bind_tools([inspect_pod], tool_choice="inspect_pod").invoke(messages)
    assert call.tool_calls and all(tc["name"] == "inspect_pod" for tc in call.tool_calls)
    messages += [call, *[
        ToolMessage(content=inspect_pod.invoke({}), tool_call_id=tc["id"])
        for tc in call.tool_calls
    ]]
    # Passing the original AIMessage back tests Gemini 4 thought signatures.
    bound = llm.bind_tools([inspect_pod])
    chunks = list(bound.stream(messages)) if streaming else []
    output = sum(chunks[1:], chunks[0]) if chunks else bound.invoke(messages)
    answer = extract_text_from_content(output.content)
    assert answer.strip() and not output.tool_calls
    for msg in [call, output]:
        assert extract_text_from_content(msg.content, include_thinking=True) == extract_text_from_content(msg.content)
    progress, answers = run_stream(workflow, chunks, output, output)
    assert answers == [answer]
    assert progress == answer
    slack = send(answers[0])
    assert answer in slack
    assert "summarize the provided thoughts" not in slack.lower()
    assert "thinking process:" not in slack.lower()
    assert "400" in answer and "256" in answer
    reasoning_tokens = sum(
        (msg.usage_metadata or {}).get("output_token_details", {}).get("reasoning", 0)
        for msg in [call, output]
    )
    print(f"{model} streaming={streaming}: tool round trip, saved answer and captured Slack clean; reasoning_tokens={reasoning_tokens}")


def test_live_carbon_safety_judge():
    from pydantic import BaseModel
    from chat.backend.agent.providers.vertex_provider import VertexAIProvider
    from utils.security.command_safety import safety_judge_thinking_overrides

    class Verdict(BaseModel):
        read_only: bool

    model = "vertex/gemini-4-carbon"
    overrides = safety_judge_thinking_overrides(model)
    assert overrides == {"thinking_level": "low", "include_thoughts": False}
    llm = VertexAIProvider().get_chat_model(model, timeout=120, max_retries=0, **overrides)
    result = llm.with_structured_output(Verdict).invoke("Is kubectl get pods read-only? Classify only; do not execute.")
    assert result.read_only is True
