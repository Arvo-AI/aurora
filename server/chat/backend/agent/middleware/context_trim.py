"""
Context safety-net middleware for LangChain create_agent.

Primary context management happens upstream — tool outputs are capped/summarized
before entering the ReAct message list (see utils/tool_output_cap.py).

This middleware provides two functions:
1. Injects correlated RCA context updates into background sessions.
2. Safety net: if accumulated messages still exceed the model's context window
   (e.g., 200+ iterations on a 200K model) or the background context budget,
   trims the oldest turns while keeping the request well-formed and cache-stable.
"""

import logging
import os
from typing import Awaitable, Callable, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from langchain_core.messages.utils import count_tokens_approximately

from ..utils.chat_context_manager import ChatContextManager
from utils.cloud.cloud_utils import get_state_context
from chat.background.context_updates import apply_rca_context_updates

logger = logging.getLogger(__name__)

# Safety net triggers at 80% of context limit — should rarely fire now that
# tool outputs are capped upstream.
_SAFETY_NET_RATIO = 0.80

# Default context budget (tokens) for background RCA runs: the conversation
# messages are trimmed past this size. The system prompt and the tool definitions
# are sent on top of it, so the request's total prompt is larger than the budget.
# 200K keeps a long investigation's per-turn input well under the 1M window of
# the Claude 5.x generation.
_DEFAULT_RCA_CONTEXT_TOKEN_BUDGET = 200_000

# When the history outgrows the limit, cut back to this fraction of it and keep
# that cut until the limit is exceeded again. A cut that slid by one tool turn per
# call would change the message prefix on every turn and defeat prompt caching:
# the whole history would be re-written at the cache-write rate each turn
# instead of being read back at the (much cheaper) cache-read rate.
_TRIM_LOW_WATERMARK_RATIO = 0.60


def rca_context_token_budget() -> int:
    """``RCA_CONTEXT_TOKEN_BUDGET`` (tokens) for background runs; unset / empty / junk /
    non-positive values mean the 200K default."""
    raw = (os.getenv("RCA_CONTEXT_TOKEN_BUDGET") or "").strip()
    if not raw:
        return _DEFAULT_RCA_CONTEXT_TOKEN_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RCA_CONTEXT_TOKEN_BUDGET
    return value if value > 0 else _DEFAULT_RCA_CONTEXT_TOKEN_BUDGET


class ContextSafetyMiddleware(AgentMiddleware):
    """Lightweight safety net for context overflow.

    With tool output capping in place, this should rarely trigger for interactive
    chat. It exists to protect small-context models (200K) during long runs (240
    iterations) where even capped messages can accumulate past the limit, and to
    enforce the background context budget.

    One instance serves one agent run (``agent.py`` builds it per
    ``agentic_tool_flow`` call), so the trim position it pins is private to that
    run's sequence of model calls.

    Args:
        model_name: Model name for context limit lookup.
        token_budget: Optional cap below the model window. Background RCA runs
            pass ``RCA_CONTEXT_TOKEN_BUDGET`` so a 240-iteration investigation on
            a 1M-token model is trimmed at ~200K tokens instead of ~760K (every
            turn re-reads the whole history, so the budget bounds the per-turn
            input bill); interactive chat passes nothing and keeps the 80% rule.
    """

    def __init__(self, model_name: str, token_budget: Optional[int] = None):
        self.model_name = model_name
        window_limit = int(
            ChatContextManager.get_context_limit(model_name) * _SAFETY_NET_RATIO
        )
        self.max_tokens = window_limit
        if token_budget is not None and token_budget > 0:
            self.max_tokens = min(window_limit, int(token_budget))
        # Id of the first kept tail message after a trim (see _trim_to_budget).
        self._cut_message_id: Optional[str] = None
        logger.info(
            f"ContextSafetyMiddleware initialized: model={model_name}, "
            f"max_tokens={self.max_tokens}"
            + (f" (token_budget={token_budget})" if token_budget else "")
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        # 1. Inject correlated incident updates into background RCA sessions.
        state = get_state_context()
        update_message = apply_rca_context_updates(state)
        if update_message:
            try:
                request = request.override(messages=[*request.messages, update_message])
                logger.info(
                    f"[ContextSafety] Appended RCA context update "
                    f"(messages={len(request.messages)})"
                )
            except Exception as e:
                logger.warning(f"[ContextSafety] Failed to append context update: {e}")

        # 2. Safety net — trim if still over budget despite upstream capping.
        estimated_tokens = count_tokens_approximately(request.messages)
        if estimated_tokens <= self.max_tokens:
            return await handler(request)

        logger.warning(
            f"[ContextSafety] Safety net triggered: {estimated_tokens} tokens "
            f"(limit: {self.max_tokens}) across {len(request.messages)} messages"
        )

        trimmed = self._trim_to_budget(list(request.messages))

        logger.info(
            f"[ContextSafety] Trimmed: {len(request.messages)} -> {len(trimmed)} messages, "
            f"~{estimated_tokens} -> ~{count_tokens_approximately(trimmed)} tokens"
        )

        return await handler(request.override(messages=trimmed))

    def _trim_to_budget(self, messages: List) -> List:
        """Trim ``messages`` under the limit, keeping the request well-formed and cache-stable.

        The leading task block — every message before the first AIMessage, i.e. the
        alert / user request the run is about — is always kept, so the request still
        opens with the user turn the model is working on and never starts with an
        assistant message (Bedrock Converse rejects such a conversation). The tail
        is cut at an AIMessage boundary, so every ToolMessage keeps the tool call it
        answers.

        Hysteresis: a new cut drops enough of the oldest turns to land under the low
        watermark, and that cut (the id of the first kept tail message) is reused on
        later calls for as long as the result fits the limit. Consecutive turns thus
        share a byte-identical prefix and the prompt cache keeps hitting between cuts.
        """
        head, rest = _split_task_head(messages)
        if not rest:
            return messages

        if self._cut_message_id is not None:
            pinned = _tail_from_message_id(rest, self._cut_message_id)
            if pinned is not None and count_tokens_approximately(head + pinned) <= self.max_tokens:
                return head + pinned

        target = int(self.max_tokens * _TRIM_LOW_WATERMARK_RATIO)
        tail = _tail_within_budget(rest, target - count_tokens_approximately(head))
        self._cut_message_id = getattr(tail[0], "id", None)
        if self._cut_message_id is None:
            logger.debug("[ContextSafety] Kept tail starts with an id-less message; cut not pinned")
        return head + tail


def _split_task_head(messages: List) -> tuple[List, List]:
    """Split at the first AIMessage: ``(task block, everything from that AIMessage on)``."""
    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            return list(messages[:i]), list(messages[i:])
    return list(messages), []


def _tail_from_message_id(messages: List, message_id: str) -> Optional[List]:
    """Suffix of ``messages`` starting at the message with ``message_id``, or ``None``."""
    for i, msg in enumerate(messages):
        if getattr(msg, "id", None) == message_id:
            return list(messages[i:])
    return None


def _tail_within_budget(messages: List, max_tokens: int) -> List:
    """Longest suffix of ``messages`` that starts at an AIMessage and fits ``max_tokens``.

    When not even the most recent AIMessage block fits, that block is returned
    anyway so the model always sees its latest tool call with its results.
    """
    best: Optional[int] = None
    last_boundary: Optional[int] = None
    suffix_tokens = 0
    for i in range(len(messages) - 1, -1, -1):
        suffix_tokens += count_tokens_approximately([messages[i]])
        if not isinstance(messages[i], AIMessage):
            continue
        if last_boundary is None:
            last_boundary = i
        if suffix_tokens > max_tokens:
            break
        best = i
    start = best if best is not None else last_boundary
    if start is None:
        return list(messages)
    return list(messages[start:])
