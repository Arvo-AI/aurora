"""Redis-backed distributed token-bucket rate limiter.

Unlike the process-local TokenBucket, this implementation coordinates across
all workers/pods via Redis atomic Lua scripts. Falls back to the local bucket
if Redis is unavailable so the app doesn't hard-fail on cache outages.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import redis

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_LUA_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = math.max(0, now - last_refill)
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 120)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 120)
    return 0
end
"""

_script_sha: Optional[str] = None


class RedisTokenBucket:
    """Distributed token bucket backed by Redis.

    Args:
        key: Redis key for this bucket (e.g. "notion:ratelimit:global").
        rate_per_sec: Tokens added per second.
        capacity: Maximum tokens the bucket can hold.
    """

    def __init__(self, key: str, rate_per_sec: float, capacity: int):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.key = key
        self.rate = rate_per_sec
        self.capacity = capacity

    def acquire(self, tokens: float = 1.0, timeout: float = 10.0) -> bool:
        """Try to acquire tokens, polling Redis until timeout.

        Returns True if tokens were granted, False on timeout.
        Returns True immediately if Redis is unavailable (fail-open).
        """
        global _script_sha
        deadline = time.monotonic() + timeout

        while True:
            rc = get_redis_client()
            if rc is None:
                return True  # fail-open

            try:
                if _script_sha is None:
                    _script_sha = rc.script_load(_LUA_SCRIPT)
                result = rc.evalsha(
                    _script_sha,
                    1,
                    self.key,
                    str(self.rate),
                    str(self.capacity),
                    str(time.time()),
                    str(tokens),
                )
                if result == 1:
                    return True
            except redis.exceptions.NoScriptError:
                _script_sha = rc.script_load(_LUA_SCRIPT)
                continue
            except Exception as exc:
                logger.debug("Redis rate limiter unavailable: %s", exc)
                return True  # fail-open

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait = min(remaining, tokens / self.rate + 0.05)
            time.sleep(wait)
