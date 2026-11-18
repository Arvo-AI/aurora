"""Small bounded OpenAI tool loop used by browser authoring and replay."""

from __future__ import annotations

import json
from typing import Any, Callable

class AgentError(RuntimeError):
    """The model did not complete a valid browser task."""


ToolExecutor = Callable[[str, dict[str, Any]], dict[str, Any]]


def run_tool_agent(
    *,
    prompt: str,
    tools: list[dict[str, Any]],
    output_name: str,
    output_schema: dict[str, Any],
    execute_tool: ToolExecutor,
    model: str,
    max_turns: int = 8,
    max_tool_calls: int = 12,
) -> dict[str, Any]:
    """Run a model with browser read tools and require structured final output."""

    from openai import OpenAI

    client = OpenAI(timeout=30.0, max_retries=1)
    history: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    used_tools = 0

    for _ in range(max_turns):
        response = client.responses.create(
            model=model,
            input=history,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            max_tool_calls=max_tool_calls,
            max_output_tokens=1600,
            store=False,
            text={
                "format": {
                    "type": "json_schema",
                    "name": output_name,
                    "strict": True,
                    "schema": output_schema,
                }
            },
        )
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            if used_tools == 0:
                raise AgentError("browser agent completed without inspecting the page")
            try:
                result = json.loads(response.output_text)
            except (TypeError, json.JSONDecodeError) as exc:
                raise AgentError("browser agent returned invalid structured output") from exc
            if not isinstance(result, dict):
                raise AgentError("browser agent output must be an object")
            return result

        outputs = []
        for call in calls:
            used_tools += 1
            if used_tools > max_tool_calls:
                raise AgentError("browser agent exceeded its tool-call limit")
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                raise AgentError("browser agent supplied invalid tool arguments") from exc
            if not isinstance(arguments, dict):
                raise AgentError("browser tool arguments must be an object")
            tool_result = execute_tool(call.name, arguments)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(tool_result, separators=(",", ":")),
                }
            )
        history.extend(item.model_dump(exclude_none=True) for item in response.output)
        history.extend(outputs)

    raise AgentError("browser agent did not finish within the turn limit")
