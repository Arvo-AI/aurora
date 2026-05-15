"""Process-local thread-safe token-bucket rate limiter.

This rate limiter lives in-process only. Under gunicorn / uvicorn / Celery,
each worker/process has its own bucket — there is no cross-process
coordination. If you need global rate limiting across workers, use Redis or
a dedicated rate-limiting service instead.

Typical usage::

    from utils.web.rate_limiter import TokenBucket

    _bucket = TokenBucket(rate_per_sec=3.0, capacity=3)

    if not _bucket.acquire(timeout=10.0):
        raise RuntimeError("Rate limit exceeded")

All operations are guarded by a ``threading.Condition`` so the bucket is
safe to share across threads.
"""

from __future__ import annotations

import threading
import time

__all__ = ["TokenBucket"]


class TokenBucket:
    """Thread-safe token bucket.

    Args:
        rate_per_sec: Tokens added per second.
        capacity: Maximum tokens the bucket can hold.
    """

    def __init__(self, rate_per_sec: float, capacity: int):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._cond = threading.Condition()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

    def acquire(self, tokens: float = 1.0, timeout: float = 10.0) -> bool:
        """Acquire ``tokens`` from the bucket, waiting up to ``timeout`` seconds.

        Returns True when the tokens were granted, False on timeout.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                self._refill_locked()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                needed = tokens - self.tokens
                wait = min(remaining, needed / self.rate + 0.01)
                self._cond.wait(timeout=wait)
