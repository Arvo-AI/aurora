"""Middleware that asks langchain-aws for per-request Converse cache points.

``ChatBedrockConverse`` (langchain-aws >= 1.9.0) accepts a request-level
``cache_control`` kwarg and, in ``_apply_cache_points``, appends a
``{"cachePoint": {"type": "default"}}`` block after the tool definitions and at
the end of the last message (plus the end of the system list when the system does
not already carry one). ``create_agent`` forwards ``ModelRequest.model_settings``
as kwargs to ``model.bind_tools(...)`` / ``model.bind(...)``, so setting
``model_settings["cache_control"]`` here is enough to reach that code path on every
turn — including the second and later ReAct turns, whose last message is the new
tool result.

Together with the manual system checkpoint built by
``utils.cache_control.build_cached_system_prompt`` a request ends up with three
cache points (tools, static system prefix, last message), under the Bedrock
maximum of four; the library counts the manual one and never exceeds the budget.
Only ``agent.py`` adds this middleware, and only when the serving provider is
Bedrock in native mode and the model is Claude-family; on any other transport the
kwarg would be unknown to the client, so it is never attached there.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest

logger = logging.getLogger(__name__)

# Anthropic-style spelling: langchain-aws reads only ``ttl`` from it and always
# emits Converse ``type: default``. Omitting ``ttl`` selects the 5-minute TTL.
DEFAULT_CACHE_CONTROL = {"type": "ephemeral"}


class BedrockPromptCacheMiddleware(AgentMiddleware):
    """Attach ``cache_control`` to every model request's ``model_settings``."""

    def __init__(self, ttl: Optional[str] = None):
        # ``ttl`` is "5m" (default, omitted) or "1h"; anything else is passed through
        # for langchain-aws to validate.
        self._cache_control: dict[str, Any] = dict(DEFAULT_CACHE_CONTROL)
        if ttl and ttl != "5m":
            self._cache_control["ttl"] = ttl

    def _patch(self, request: ModelRequest) -> ModelRequest:
        settings = dict(getattr(request, "model_settings", None) or {})
        if "cache_control" in settings:
            # Another layer already decided; never override an explicit setting.
            return request
        settings["cache_control"] = dict(self._cache_control)
        override = getattr(request, "override", None)
        if callable(override):
            return override(model_settings=settings)
        request.model_settings = settings
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._patch(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._patch(request))
