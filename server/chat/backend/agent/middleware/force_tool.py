"""Middleware that forces a specific tool call on the first LLM turn."""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest


class ForceToolChoice(AgentMiddleware):
    """Set tool_choice on the first model invocation, then step aside."""

    def __init__(self, tool_name: str):
        self._tool_name = tool_name
        self._fired = False

    def _tool_choice_for(self, model) -> object:
        """Return the provider-native `tool_choice` value forcing a single tool.

        Direct providers each use their own shape; LLM_PROVIDER_MODE=openrouter
        is OpenAI-compatible so the OpenAI shape works universally there.
        """
        llm_type = getattr(model, "_llm_type", "") or ""
        # Unwrap RunnableBinding / similar (request.model may be wrapped)
        inner = getattr(model, "bound", None)
        if inner is not None and not llm_type:
            llm_type = getattr(inner, "_llm_type", "") or ""

        if "anthropic" in llm_type:
            return {"type": "tool", "name": self._tool_name}
        # OpenAI (direct + openrouter), default for unknown providers.
        return {
            "type": "function",
            "function": {"name": self._tool_name},
        }

    def _patch(self, request: ModelRequest) -> ModelRequest:
        if not self._fired:
            self._fired = True
            request.tool_choice = self._tool_choice_for(request.model)
        return request

    def wrap_model_call(self, request, call_next):
        return call_next(self._patch(request))

    async def awrap_model_call(self, request, call_next):
        return await call_next(self._patch(request))
