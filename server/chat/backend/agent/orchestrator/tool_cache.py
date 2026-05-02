"""Redis-backed per-incident tool result cache for the multi-agent RCA orchestrator."""

import hashlib
import json
import logging
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Coroutine, Optional

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 600   # seconds — standard tool results
_VOLATILE_TTL = 60   # seconds — results that may change quickly
_KEY_PREFIX = "rca_tool_cache"
_HIT_COUNTER_LIMIT = 1024  # bound long-running worker memory

_hit_counters: "OrderedDict[str, int]" = OrderedDict()


def _get_redis():
    return get_redis_client()


def _cache_key(incident_id: str, tool_name: str, args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, default=str)
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
            from utils.cloud.cloud_utils import _state_var
            state = _state_var.get()
            if state is not None:
                incident_id = getattr(state, "incident_id", None)
        except Exception:
            pass

        if not incident_id:
            return await coro_fn(*args, **kwargs)

        key = _cache_key(incident_id, tool_name, kwargs)
        client = _get_redis()
        if client:
            try:
                cached = client.get(key)
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
                client.setex(key, ttl_seconds, serialized)
            except Exception as exc:
                logger.debug("RCA tool cache set error: %s", exc)
        return result
    return wrapper
