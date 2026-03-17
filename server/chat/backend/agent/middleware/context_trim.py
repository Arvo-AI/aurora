"""
Context trimming middleware for LangChain create_agent.

Prevents context overflow during long-running ReAct loops (e.g., RCA investigations
with 240 recursion limit) by trimming messages before each LLM call. Only affects
what the LLM sees — the full message history is preserved in graph state.
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

# Use 75% of the model's context limit, leaving headroom for the response
_CONTEXT_USAGE_RATIO = 0.75

# Max characters to keep per tool output when truncating oversized messages.
# ~25K tokens — enough to preserve useful context from a single tool call.
_MAX_TOOL_CONTENT_CHARS = 100_000


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

        # Step 1: Try trim_messages to keep the most recent messages that fit.
        trimmed = trim_messages(
            request.messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=self.max_tokens,
        )

        # Step 2: Drop orphaned ToolMessages from the front (no matching tool_use).
        trimmed = _drop_orphaned_tool_messages(trimmed)

        # Step 3: If empty (individual messages bigger than budget), take last 6
        # messages and truncate their content to fit.
        if not trimmed:
            logger.warning("Trimming produced empty list, truncating last messages to fit")
            trimmed = _drop_orphaned_tool_messages(request.messages[-6:])

        # Step 4: If still over budget (huge tool outputs), truncate content.
        trimmed_tokens = count_tokens_approximately(trimmed)
        if trimmed_tokens > self.max_tokens:
            logger.warning(
                f"Trimmed messages still over budget ({trimmed_tokens} > {self.max_tokens}), "
                "truncating oversized message content"
            )
            trimmed = _truncate_oversized_content(trimmed, self.max_tokens)

        trimmed_count = len(trimmed)
        trimmed_tokens = count_tokens_approximately(trimmed)

        logger.info(
            f"Context trimmed: {original_count} -> {trimmed_count} messages, "
            f"~{estimated_tokens} -> ~{trimmed_tokens} tokens"
        )

        return await handler(request.override(messages=trimmed))


def _drop_orphaned_tool_messages(messages: List) -> List:
    """Drop ToolMessages from the front that have no matching AIMessage with tool_calls.

    After trim_messages cuts from the beginning, the first message(s) can be
    ToolMessages whose parent AIMessage (containing tool_calls) was trimmed.
    Sending these to the Anthropic API causes:
      "Each tool_result block must have a corresponding tool_use block
       in the previous message."
    """
    # Collect tool_call IDs from all AIMessages in the list
    available_tool_call_ids: set = set()
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    available_tool_call_ids.add(tc_id)

    # Drop leading ToolMessages whose tool_call_id is not in the set
    start = 0
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id not in available_tool_call_ids:
                start = i + 1
                continue
        break

    if start > 0:
        logger.info(f"Dropped {start} orphaned ToolMessage(s) from trimmed context")

    return messages[start:]


def _truncate_oversized_content(messages: List, max_tokens: int) -> List:
    """Truncate oversized message content (especially ToolMessages) to fit the budget.

    When individual tool outputs are larger than the entire token budget,
    trim_messages can't help — we must truncate the content itself.
    """
    result = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
            if len(msg.content) > _MAX_TOOL_CONTENT_CHARS:
                truncated = ToolMessage(
                    content=msg.content[:_MAX_TOOL_CONTENT_CHARS] + "\n\n[Truncated for context window]",
                    tool_call_id=msg.tool_call_id,
                    name=getattr(msg, "name", None),
                )
                result.append(truncated)
                continue
        result.append(msg)

    # If still over budget after truncating tool outputs, progressively
    # shorten ALL message content until it fits.
    tokens = count_tokens_approximately(result)
    if tokens > max_tokens:
        # Calculate how much we need to cut per-message
        chars_per_msg = (max_tokens * 4) // max(len(result), 1)  # ~4 chars per token
        squeezed = []
        for msg in result:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) > chars_per_msg:
                if isinstance(msg, ToolMessage):
                    squeezed.append(ToolMessage(
                        content=content[:chars_per_msg] + "\n\n[Truncated for context window]",
                        tool_call_id=msg.tool_call_id,
                        name=getattr(msg, "name", None),
                    ))
                elif isinstance(msg, AIMessage):
                    squeezed.append(AIMessage(content=content[:chars_per_msg]))
                else:
                    squeezed.append(msg)
            else:
                squeezed.append(msg)
        result = squeezed

    return result
