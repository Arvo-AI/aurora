"""Redis-backed per-incident tool result cache for the multi-agent RCA orchestrator."""

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Coroutine, Optional

from utils.cache.redis_client import get_redis_client
from utils.cloud.cloud_utils import _state_var

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 600   # seconds — standard tool results
_KEY_PREFIX = "rca_tool_cache"
_HIT_COUNTER_LIMIT = 1024  # bound long-running worker memory

_hit_counters: "OrderedDict[str, int]" = OrderedDict()


def _get_redis():
    return get_redis_client()


def _cache_key(incident_id: str, tool_name: str, args: tuple, kwargs: dict) -> str:
    canonical = json.dumps(
        {"args": list(args), "kwargs": kwargs}, sort_keys=True, default=str
    )
    digest = hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()
    return f"{_KEY_PREFIX}:{incident_id}:{digest}"


def get_cache_hit_count(incident_id: str) -> int:
    return _hit_counters.get(incident_id, 0)


def cache_decorator(coro_fn: Callable[..., Coroutine], *, tool_name: str,
                    ttl_seconds: int = _DEFAULT_TTL) -> Callable[..., Coroutine]:
    @wraps(coro_fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        incident_id: Optional[str] = None
        try:
            state = _state_var.get()
            if state is not None:
                incident_id = getattr(state, "incident_id", None)
        except Exception:
            logger.debug("RCA tool cache: failed to read incident state", exc_info=True)

        if not incident_id:
            return await coro_fn(*args, **kwargs)

        key = _cache_key(incident_id, tool_name, args, kwargs)
        try:
            client = _get_redis()
        except Exception:
            logger.exception("RCA tool cache: failed to initialize Redis client")
            client = None
        if client:
            try:
                cached = await asyncio.to_thread(client.get, key)
                if cached is not None:
                    _hit_counters[incident_id] = _hit_counters.get(incident_id, 0) + 1
                    _hit_counters.move_to_end(incident_id)
                    while len(_hit_counters) > _HIT_COUNTER_LIMIT:
                        _hit_counters.popitem(last=False)
                    logger.debug(
                        "RCA tool cache HIT: incident=%s tool=%s hits=%d",
                        incident_id, tool_name, _hit_counters[incident_id],
                    )
                    return json.loads(cached)
            except Exception as exc:
                logger.debug("RCA tool cache get error: %s", exc)

        result = await coro_fn(*args, **kwargs)
        if client:
            try:
                serialized = json.dumps(result, default=str)
                await asyncio.to_thread(client.setex, key, ttl_seconds, serialized)
            except Exception as exc:
                logger.debug("RCA tool cache set error: %s", exc)
        return result
    return wrapper


def wrap_tool_with_cache(tool: Any, *, tool_metadata: dict) -> Any:
    """Return a copy of ``tool`` whose async coroutine is wrapped with the
    per-incident Redis cache, when metadata marks the tool as safely cacheable.

    Metadata gate: ``cacheable=True`` AND ``mutates=False``. Anything else is
    returned unmodified. Original tool is never mutated — sub-agent paths share
    the singleton list returned by ``get_cloud_tools()``.
    """
    if not tool_metadata.get("cacheable") or tool_metadata.get("mutates"):
        return tool
    coroutine = getattr(tool, "coroutine", None)
    if coroutine is None or not asyncio.iscoroutinefunction(coroutine):
        return tool
    try:
        wrapped = cache_decorator(coroutine, tool_name=getattr(tool, "name", "unknown"))
        return tool.model_copy(update={"coroutine": wrapped})
    except (AttributeError, TypeError):
        logger.exception(
            "RCA tool cache: failed to wrap tool=%s; returning unwrapped",
            getattr(tool, "name", "?"),
        )
        return tool
