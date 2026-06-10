"""Middleware that forces a specific tool call on the first LLM turn."""

from __future__ import annotations

from collections import deque
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest


# (module_substring, class_name, llm_type_substring, provider_key)
# OpenRouter uses ChatOpenAI with a custom base URL — the "openai" match
# is intentional: OpenRouter's API is OpenAI-shaped so tool_choice format
# is identical.
_PROVIDER_SIGNATURES: list[tuple[str, str, str, str]] = [
    ("langchain_anthropic", "chatanthropic", "anthropic", "anthropic"),
    ("langchain_google_vertexai", "chatvertexai", "vertexai", "vertex"),
    ("langchain_google", "chatgooglegenerativeai", "google", "google"),
    ("langchain_aws", "chatbedrockconverse", "bedrock", "bedrock"),
    ("langchain_openai", "chatopenai", "openai", "openai"),
]


class ForceToolChoice(AgentMiddleware):
    """Set tool_choice on the first model invocation, then step aside."""

    def __init__(self, tool_name: str, provider: str | None = None):
        self._tool_name = tool_name
        self._provider = provider
        self._fired = False

    @staticmethod
    def _match_provider(module: str, name: str, llm_type: str) -> str | None:
        for mod_key, cls_key, type_key, provider in _PROVIDER_SIGNATURES:
            if mod_key in module or name == cls_key or type_key in llm_type:
                return provider
        return None

    @staticmethod
    def _infer_provider(model: Any) -> str | None:
        """Best-effort provider detection from a LangChain model instance.

        Normally a fallback — agent.py passes provider= explicitly. The exception is
        the dual-mode "bedrock" provider, whose gateway (ChatOpenAI) and native
        (ChatBedrockConverse) clients share provider="bedrock"; _tool_choice calls
        this to tell the two apart and pick the right tool_choice format.
        """
        seen: set[int] = set()
        candidates: deque[Any] = deque([model])

        while candidates:
            candidate = candidates.popleft()
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))

            module = candidate.__class__.__module__.lower()
            name = candidate.__class__.__name__.lower()
            llm_type = str(getattr(candidate, "_llm_type", "")).lower()

            matched = ForceToolChoice._match_provider(module, name, llm_type)
            if matched:
                return matched

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
        if provider == "bedrock":
            # Gateway (ChatOpenAI) and native (ChatBedrockConverse) both report
            # provider="bedrock". Only the gateway — positively detected as ChatOpenAI
            # — wants OpenAI-style. Native Converse wants the Bedrock toolChoice shape
            # {"tool": {"name": ...}}; its first key must be "tool" (not "type"), or
            # ChatBedrockConverse rejects it as an unsupported tool_choice type.
            if self._infer_provider(getattr(request, "model", None)) == "openai":
                return {
                    "type": "function",
                    "function": {"name": self._tool_name},
                }
            return {"tool": {"name": self._tool_name}}
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
