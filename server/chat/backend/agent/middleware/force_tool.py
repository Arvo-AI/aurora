"""Middleware that forces a specific tool call on the first LLM turn."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest


class ForceToolChoice(AgentMiddleware):
    """Set tool_choice on the first model invocation, then step aside."""

    def __init__(self, tool_name: str, provider: str | None = None):
        self._tool_name = tool_name
        self._provider = provider
        self._fired = False

    @staticmethod
    def _infer_provider(model: Any) -> str | None:
        seen: set[int] = set()
        candidates = [model]

        while candidates:
            candidate = candidates.pop(0)
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))

            module = candidate.__class__.__module__.lower()
            name = candidate.__class__.__name__.lower()
            llm_type = str(getattr(candidate, "_llm_type", "")).lower()

            if (
                "langchain_anthropic" in module
                or name == "chatanthropic"
                or "anthropic" in llm_type
            ):
                return "anthropic"
            if (
                "langchain_google" in module
                or name == "chatgooglegenerativeai"
                or "google" in llm_type
            ):
                return "google"
            if (
                "langchain_openai" in module
                or name == "chatopenai"
                or "openai" in llm_type
            ):
                return "openai"

            for attr in ("bound", "model", "runnable"):
                nested = getattr(candidate, attr, None)
                if nested is not candidate:
                    candidates.append(nested)

        return None

    def _tool_choice(self, request: ModelRequest):
        provider = (
            self._provider
            or self._infer_provider(getattr(request, "model", None))
            or ""
        ).lower()

        if provider == "anthropic":
            return {"type": "tool", "name": self._tool_name}
        if provider in {"google", "vertex"}:
            return self._tool_name
        return {
            "type": "function",
            "function": {"name": self._tool_name},
        }

    def _patch(self, request: ModelRequest) -> ModelRequest:
        if not self._fired:
            self._fired = True
            tool_choice = self._tool_choice(request)
            override = getattr(request, "override", None)
            if callable(override):
                return override(tool_choice=tool_choice)
            request.tool_choice = tool_choice
        return request

    def wrap_model_call(self, request, call_next):
        return call_next(self._patch(request))

    async def awrap_model_call(self, request, call_next):
        return await call_next(self._patch(request))
