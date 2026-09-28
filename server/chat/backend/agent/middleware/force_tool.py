"""Middleware that forces a specific tool call on the first LLM turn."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest

logger = logging.getLogger(__name__)


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

# Newer Claude models (Opus 5.5, Fable 5.1) removed forced tool selection and answer
# `400 tool_choice: type "tool" and "any" are not supported for this model.` on the
# direct API, with a matching Bedrock Converse ValidationException. Rather than keep a
# per-model denylist, we learn it from the model's own error (same approach as
# providers/_sampling_guard) and fall back to tool_choice=auto plus an explicit prompt
# directive. Remembered per process so only the first forced turn per model pays the
# rejected round-trip.
_FORCE_UNSUPPORTED: set[str] = set()

# Substrings that must both appear for an error to count as "this model won't be forced".
_TOOL_CHOICE_MARKERS = ("tool_choice", "toolchoice", "tool choice")
_REJECTION_MARKERS = (
    "not supported",
    "not support",
    "unsupported",
    "is not valid",
    "invalid",
)

_FALLBACK_DIRECTIVE = (
    "SYSTEM DIRECTIVE: this turn was started by an explicit UI action, not by a "
    "question. Your entire reply must be a single call to the `{tool_name}` tool — "
    "no prose, no other tool, no follow-up question. Infer any missing arguments "
    "from the conversation above."
)


def _model_label(model: Any) -> str:
    """Best-effort model id, unwrapping runnable wrappers the way ``_infer_provider`` does.

    Used only as a cache key for the forced-tool rejection, so an empty string
    (unknown model) simply disables the cache rather than breaking the call.
    """
    seen: set[int] = set()
    candidates: deque[Any] = deque([model])

    while candidates:
        candidate = candidates.popleft()
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))

        for attr in ("model_name", "model_id", "model"):
            value = getattr(candidate, attr, None)
            if isinstance(value, str) and value:
                return value

        for attr in ("bound", "model", "runnable"):
            nested = getattr(candidate, attr, None)
            if nested is not candidate:
                candidates.append(nested)

    return ""


def _rejects_forced_tool_choice(err: BaseException) -> bool:
    """True when ``err`` is the provider saying it won't honour a forced tool_choice.

    Both a tool_choice mention and a rejection phrase are required so unrelated 400s
    (bad tool schema, context overflow) still surface to the caller unchanged.
    """
    msg = str(err).lower()
    return any(m in msg for m in _TOOL_CHOICE_MARKERS) and any(
        m in msg for m in _REJECTION_MARKERS
    )


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

    @staticmethod
    def _nudged_messages(messages: Any, tool_name: str) -> Any:
        """Append the "you must call this tool" directive as a trailing human turn.

        Used only on the ``auto`` fallback path: without a forced tool_choice the model
        needs the instruction in-band, or a UI-initiated action silently returns prose.
        """
        from langchain_core.messages import HumanMessage

        directive = HumanMessage(content=_FALLBACK_DIRECTIVE.format(tool_name=tool_name))
        if isinstance(messages, list):
            return [*messages, directive]
        return messages

    def _fallback_request(self, request: ModelRequest) -> ModelRequest:
        """Rebuild ``request`` with tool_choice=auto plus the in-prompt directive."""
        messages = self._nudged_messages(getattr(request, "messages", None), self._tool_name)
        override = getattr(request, "override", None)
        if callable(override):
            # ``messages`` may be absent on some ModelRequest versions — only override
            # what we actually resolved, so a missing attribute doesn't blow up here.
            kwargs: dict[str, Any] = {"tool_choice": "auto"}
            if messages is not None:
                kwargs["messages"] = messages
            return override(**kwargs)
        request.tool_choice = "auto"
        if messages is not None:
            request.messages = messages
        return request

    def _should_force(self, request: ModelRequest) -> bool:
        """Skip forcing entirely for a model already known to reject it."""
        label = _model_label(getattr(request, "model", None))
        return not (label and label in _FORCE_UNSUPPORTED)

    def _remember_rejection(self, request: ModelRequest) -> None:
        label = _model_label(getattr(request, "model", None))
        if label:
            _FORCE_UNSUPPORTED.add(label)
        logger.warning(
            "Model %s rejected a forced tool_choice for '%s'; retrying with "
            "tool_choice=auto and an explicit prompt directive.",
            label or "<unknown>", self._tool_name,
        )

    def wrap_model_call(self, request, call_next):
        # Already fired (turn 2+) — this middleware is done, pass through untouched.
        if self._fired:
            return call_next(request)

        # Model is known to reject forcing — go straight to the auto + prompt-directive
        # path and skip the guaranteed 400.
        if not self._should_force(request):
            self._fired = True
            return call_next(self._fallback_request(request))

        try:
            return call_next(self._patch(request))
        except Exception as err:
            if not _rejects_forced_tool_choice(err):
                raise
            self._remember_rejection(request)
            return call_next(self._fallback_request(request))

    async def awrap_model_call(self, request, call_next):
        # Same three-path logic as the sync version; see wrap_model_call.
        if self._fired:
            return await call_next(request)

        if not self._should_force(request):
            self._fired = True
            return await call_next(self._fallback_request(request))

        try:
            return await call_next(self._patch(request))
        except Exception as err:
            if not _rejects_forced_tool_choice(err):
                raise
            self._remember_rejection(request)
            return await call_next(self._fallback_request(request))
