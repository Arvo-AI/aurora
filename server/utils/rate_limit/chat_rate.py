"""Per-user chat rate limiter backed by Redis.

Keyed on user_id (not WebSocket id) so the limit applies across transports
(WebSocket and SSE) and across reconnects. Uses a 60-second fixed window.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_BUCKET_SECONDS = 60
_KEY_PREFIX = "chat:rate"


def is_allowed(user_id: str, limit_per_minute: int = 60) -> bool:
    """Return True if the user is under the per-minute limit.

    Fail-open if Redis is unavailable so a transient cache outage doesn't lock
    every user out of chat. Logs a warning so this is observable.
    """
    if not user_id:
        return True

    try:
        from utils.cache.redis_client import get_redis_client
        client = get_redis_client()
    except Exception as e:
        logger.warning("rate_limit: redis client init failed (%s); fail-open", e)
        return True

    if client is None:
        logger.warning("rate_limit: redis unavailable; fail-open for user=%s", user_id)
        return True

    key = f"{_KEY_PREFIX}:{user_id}"
    try:
        count = client.incr(key)
        if count == 1:
            client.expire(key, _BUCKET_SECONDS)
        elif client.ttl(key) == -1:
            # Self-heal: a previous EXPIRE failed, so the key has no TTL and
            # would otherwise grow unbounded. Reset the bucket window.
            client.expire(key, _BUCKET_SECONDS)
    except Exception as e:
        logger.warning("rate_limit: redis incr/expire failed (%s); fail-open", e)
        return True

    try:
        count_int = int(count)
    except (TypeError, ValueError) as e:
        logger.warning(
            "rate_limit: incr returned non-int %r for user=%s (%s); fail-open",
            count, user_id, e,
        )
        return True

    return count_int <= limit_per_minute
