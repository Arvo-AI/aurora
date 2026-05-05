"""Middleware that forces a specific tool call on the first LLM turn."""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest


class ForceToolChoice(AgentMiddleware):
    """Set tool_choice on the first model invocation, then step aside."""

    def __init__(self, tool_name: str):
        self._tool_name = tool_name
        self._fired = False

    def _patch(self, request: ModelRequest) -> ModelRequest:
        if not self._fired:
            self._fired = True
            request.tool_choice = {
                "type": "function",
                "function": {"name": self._tool_name},
            }
        return request

    def wrap_model_call(self, request, call_next):
        return call_next(self._patch(request))

    async def awrap_model_call(self, request, call_next):
        return await call_next(self._patch(request))
