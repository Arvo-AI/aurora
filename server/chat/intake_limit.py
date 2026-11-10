"""Cap on background investigations started per user.

Alert webhooks, manual RCA, and action runs share one Redis counter.
The default is 20 per 5 minutes. A deployment with a higher peak sets
BACKGROUND_CHAT_RATE_LIMIT_MAX.
"""

import logging
import os

logger = logging.getLogger(__name__)

# 20 covers a busy burst of about 10 investigations per window, and still
# stops a runaway alert flood from filling the worker queue.
DEFAULT_MAX_REQUESTS = 20
WINDOW_SECONDS = 300
MIN_MAX_REQUESTS = 1
# A typo must not turn the cap into an unbounded investigation launch.
MAX_MAX_REQUESTS = 1000

_MAX_ENV = "BACKGROUND_CHAT_RATE_LIMIT_MAX"


def background_chat_rate_limit_max() -> int:
    """Max investigations one user may start per 5-minute window."""
    raw = (os.getenv(_MAX_ENV) or "").strip()
    # Unset and blank both mean the shipped default.
    if not raw:
        return DEFAULT_MAX_REQUESTS
    try:
        value = int(raw)
    except ValueError:
        # A typo must not disable the cap or reject every investigation.
        logger.warning(
            "%s=%r is not an integer; using %s",
            _MAX_ENV,
            raw,
            DEFAULT_MAX_REQUESTS,
        )
        return DEFAULT_MAX_REQUESTS
    # Zero or negative would reject intake, including a single page.
    if value < MIN_MAX_REQUESTS:
        logger.warning(
            "%s=%s is below %s; using %s",
            _MAX_ENV,
            value,
            MIN_MAX_REQUESTS,
            MIN_MAX_REQUESTS,
        )
        return MIN_MAX_REQUESTS
    # Above the ceiling, one alert storm can fill the worker queue for hours.
    if value > MAX_MAX_REQUESTS:
        logger.warning(
            "%s=%s is above %s; using %s",
            _MAX_ENV,
            value,
            MAX_MAX_REQUESTS,
            MAX_MAX_REQUESTS,
        )
        return MAX_MAX_REQUESTS
    return value


def consume_background_chat_slot(user_id: str, redis_client) -> bool:
    """Take one slot. False when this user is already over the cap."""
    max_requests = background_chat_rate_limit_max()
    key = f"background_chat_rate_limit:{user_id}"
    count = redis_client.incr(key)
    # Later hits must not refresh the TTL, or a steady stream never expires.
    if count == 1:
        redis_client.expire(key, WINDOW_SECONDS)
    # Past the cap, another investigation is a flood, not one more page.
    if count > max_requests:
        ttl = redis_client.ttl(key)
        logger.warning(
            "[BackgroundChat] Rate limited user %s - %s/%s requests, %ss remaining",
            user_id,
            count,
            max_requests,
            ttl,
        )
        return False
    logger.debug(
        "[BackgroundChat] User %s request %s/%s",
        user_id,
        count,
        max_requests,
    )
    return True
