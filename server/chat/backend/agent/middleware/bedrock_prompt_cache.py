"""Middleware that adds Bedrock cachePoints after the tools and the last message.

With the system checkpoint from ``build_cached_system_prompt`` a native Bedrock
request carries three cache points (tools, static system prefix, last message),
under Bedrock's cap of four. Attached only when the serving provider's cache
style is ``CACHE_STYLE_BEDROCK``. Must be the innermost middleware so the points
mark the final request.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest

from chat.backend.agent.utils.cache_control import (
    with_bedrock_last_message_cache_point,
    with_bedrock_tools_cache_point,
)


class BedrockPromptCacheMiddleware(AgentMiddleware):
    """Mark tool definitions and conversation history as cacheable on every Bedrock turn."""

    @staticmethod
    def _patch(request: ModelRequest) -> ModelRequest:
        tools = with_bedrock_tools_cache_point(list(getattr(request, "tools", None) or []))
        messages = with_bedrock_last_message_cache_point(list(getattr(request, "messages", None) or []))
        override = getattr(request, "override", None)
        if callable(override):
            return override(tools=tools, messages=messages)
        request.tools = tools
        request.messages = messages
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._patch(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._patch(request))
