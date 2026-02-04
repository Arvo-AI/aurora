"""
Context trimming middleware for LangChain create_agent.

Prevents context overflow during long-running ReAct loops (e.g., RCA investigations
with 240 recursion limit) by trimming messages before each LLM call. Only affects
what the LLM sees — the full message history is preserved in graph state.
"""

import logging
from typing import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages.utils import trim_messages, count_tokens_approximately

from ..utils.chat_context_manager import ChatContextManager
from utils.cloud.cloud_utils import get_state_context
from chat.background.context_updates import apply_rca_context_updates

logger = logging.getLogger(__name__)

# Use 75% of the model's context limit, leaving headroom for the response
_CONTEXT_USAGE_RATIO = 0.75


class ContextTrimMiddleware(AgentMiddleware):
    """Trims messages before each LLM call to prevent context overflow.

    During a ReAct agent loop, tool outputs accumulate in the message list
    without any trimming between iterations. With a high recursion limit
    (e.g., 240 for RCA), the context can grow far beyond the model's limit.

    This middleware intercepts each LLM call via awrap_model_call and trims
    the ModelRequest.messages to fit within the model's context window.
    Because it modifies the request (not the graph state), the full message
    history is preserved for persistence.

    Args:
        model_name: Model name in OpenRouter format (e.g., "anthropic/claude-opus-4.5").
            Used to look up the context limit from ChatContextManager.MODEL_CONTEXT_LIMITS.
    """

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.max_tokens = int(
            ChatContextManager.get_context_limit(model_name) * _CONTEXT_USAGE_RATIO
        )
        logger.info(
            f"ContextTrimMiddleware initialized: model={model_name}, "
            f"max_tokens={self.max_tokens}"
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        # Inject correlated incident updates into background RCA sessions.
        # Strategy: Append at the END as the most recent message (highest priority).
        # Only inject ONCE when the update first arrives.
        state = get_state_context()
        update_message = apply_rca_context_updates(state)
        if update_message:
            try:
                # Append at end - most recent message has highest priority
                request = request.override(messages=[*request.messages, update_message])
                logger.info(
                    "[ContextTrimMiddleware] Appended context update to request "
                    f"(messages={len(request.messages)})"
                )
            except Exception as e:
                logger.warning("[ContextTrimMiddleware] Failed to append context update: %s", e)

        original_count = len(request.messages)
        estimated_tokens = count_tokens_approximately(request.messages)

        if estimated_tokens <= self.max_tokens:
            return await handler(request)

        logger.warning(
            f"Context trimming triggered: {estimated_tokens} tokens "
            f"(limit: {self.max_tokens}) across {original_count} messages"
        )

        trimmed = trim_messages(
            request.messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=self.max_tokens,
            start_on="human",
            end_on=("human", "tool"),
        )

        trimmed_count = len(trimmed)
        trimmed_tokens = count_tokens_approximately(trimmed)

        logger.info(
            f"Context trimmed: {original_count} -> {trimmed_count} messages, "
            f"~{estimated_tokens} -> ~{trimmed_tokens} tokens"
        )

        return await handler(request.override(messages=trimmed))
