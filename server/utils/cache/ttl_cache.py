"""Small, thread-safe, bounded TTL cache (stdlib only).

Used as the in-process fallback for short-lived verdict / dedup keys when Redis
is unavailable. Kept dependency-free on purpose: ``utils.security`` and the
Celery tasks import it, and the CI pre-checks job runs with a minimal
dependency set that has neither ``cachetools`` nor ``redis``.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional


class TTLCache:
    """``get`` / ``set`` with a per-entry expiry and an LRU bound on the entry count."""

    _MISSING = object()

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0, timer: Callable[[], float] = time.monotonic):
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.ttl = float(ttl)
        self._timer = timer
        self._lock = threading.Lock()
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        # Fast path on every write: drop expired entries from the front, where the
        # oldest insertions sit. With mixed per-entry TTLs an expired entry can hide
        # behind a longer-lived one, so this is a best-effort sweep; ``_purge_all``
        # is the exact one (used where the count must be right).
        while self._data:
            _, (expires_at, _) = next(iter(self._data.items()))
            if expires_at > now:
                break
            self._data.popitem(last=False)

    def _purge_all(self, now: float) -> None:
        # Full scan, bounded by maxsize: removes every expired entry wherever it sits.
        for key in [k for k, (expires_at, _) in self._data.items() if expires_at <= now]:
            del self._data[key]

    def get(self, key: Hashable, default: Any = None) -> Any:
        now = self._timer()
        with self._lock:
            item = self._data.get(key, self._MISSING)
            if item is self._MISSING:
                return default
            expires_at, value = item
            if expires_at <= now:
                del self._data[key]
                return default
            return value

    def set(self, key: Hashable, value: Any, ttl: Optional[float] = None) -> None:
        now = self._timer()
        expires_at = now + (self.ttl if ttl is None else float(ttl))
        with self._lock:
            self._purge_expired(now)
            if key in self._data:
                del self._data[key]
            self._data[key] = (expires_at, value)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def add(self, key: Hashable, value: Any, ttl: Optional[float] = None) -> bool:
        """Set only if the key is absent (or expired). Returns True when it was added —
        the in-process equivalent of Redis ``SET key value NX EX ttl``."""
        now = self._timer()
        with self._lock:
            item = self._data.get(key, self._MISSING)
            if item is not self._MISSING and item[0] > now:
                return False
            self._purge_expired(now)
            if key in self._data:
                del self._data[key]
            self._data[key] = (now + (self.ttl if ttl is None else float(ttl)), value)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
            return True

    def discard(self, key: Hashable) -> None:
        """Remove ``key`` if present (no error when absent) — the in-process
        equivalent of Redis ``DEL``, used to release a claim ``add`` made."""
        with self._lock:
            self._data.pop(key, None)

    def __contains__(self, key: Hashable) -> bool:
        return self.get(key, self._MISSING) is not self._MISSING

    def __len__(self) -> int:
        """Number of live entries: every expired entry is purged first, wherever it sits."""
        now = self._timer()
        with self._lock:
            self._purge_all(now)
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
