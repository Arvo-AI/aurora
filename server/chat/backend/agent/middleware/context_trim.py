"""
Context safety-net middleware for LangChain create_agent.

Primary context management happens upstream — tool outputs are capped/summarized
before entering the ReAct message list (see utils/tool_output_cap.py).

This middleware provides two functions:
1. Injects correlated RCA context updates into background sessions.
2. Safety net: if accumulated messages still exceed the model's context window
   (e.g., 200+ iterations on a 200K model), trims to the most recent messages.
"""

import logging
from typing import Awaitable, Callable, List

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.utils import trim_messages, count_tokens_approximately

from ..utils.chat_context_manager import ChatContextManager
from utils.cloud.cloud_utils import get_state_context
from chat.background.context_updates import apply_rca_context_updates

logger = logging.getLogger(__name__)

# Safety net triggers at 80% of context limit — should rarely fire now that
# tool outputs are capped upstream.
_SAFETY_NET_RATIO = 0.80


class ContextSafetyMiddleware(AgentMiddleware):
    """Lightweight safety net for context overflow.

    With tool output capping in place, this should rarely trigger. It exists
    to protect small-context models (200K) during long runs (240 iterations)
    where even capped messages can accumulate past the limit.

    Args:
        model_name: Model name for context limit lookup.
    """

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.max_tokens = int(
            ChatContextManager.get_context_limit(model_name) * _SAFETY_NET_RATIO
        )
        logger.info(
            f"ContextSafetyMiddleware initialized: model={model_name}, "
            f"max_tokens={self.max_tokens}"
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

        trimmed = trim_messages(
            request.messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=self.max_tokens,
        )

        # Drop orphaned ToolMessages whose AIMessage (tool_use) was trimmed.
        trimmed = _drop_orphaned_tool_messages(trimmed)

        if not trimmed:
            logger.warning("[ContextSafety] Trim produced empty list, keeping last 6 messages")
            trimmed = _drop_orphaned_tool_messages(request.messages[-6:])

        logger.info(
            f"[ContextSafety] Trimmed: {len(request.messages)} -> {len(trimmed)} messages, "
            f"~{estimated_tokens} -> ~{count_tokens_approximately(trimmed)} tokens"
        )

        return await handler(request.override(messages=trimmed))


def _drop_orphaned_tool_messages(messages: List) -> List:
    """Drop ToolMessages from the front that have no matching AIMessage with tool_calls."""
    available_tool_call_ids: set = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    available_tool_call_ids.add(tc_id)

    start = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id not in available_tool_call_ids:
                start = i + 1
                continue
        break

    if start > 0:
        logger.info(f"[ContextSafety] Dropped {start} orphaned ToolMessage(s)")

    return messages[start:]
