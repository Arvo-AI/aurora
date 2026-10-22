"""Bounded in-process TTL cache (utils.cache.ttl_cache), the Redis fallback for
the command-safety verdict cache and the memory-extraction debounce."""

import pytest

from utils.cache.ttl_cache import TTLCache


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock():
    return _Clock()


def test_set_get_roundtrip(clock):
    cache = TTLCache(maxsize=4, ttl=10, timer=clock)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    assert "k" in cache
    assert len(cache) == 1


def test_entries_expire(clock):
    cache = TTLCache(maxsize=4, ttl=10, timer=clock)
    cache.set("k", "v")
    clock.now += 9.9
    assert cache.get("k") == "v"
    clock.now += 0.2
    assert cache.get("k") is None
    assert cache.get("k", "default") == "default"
    assert len(cache) == 0


def test_per_entry_ttl_override(clock):
    cache = TTLCache(maxsize=4, ttl=10, timer=clock)
    cache.set("short", 1, ttl=1)
    cache.set("long", 2, ttl=100)
    clock.now += 5
    assert cache.get("short") is None
    assert cache.get("long") == 2


def test_lru_bound_evicts_oldest(clock):
    cache = TTLCache(maxsize=2, ttl=100, timer=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2 and cache.get("c") == 3
    assert len(cache) == 2


def test_add_is_set_nx(clock):
    cache = TTLCache(maxsize=4, ttl=10, timer=clock)
    assert cache.add("k", 1) is True
    assert cache.add("k", 2) is False
    assert cache.get("k") == 1
    clock.now += 11
    assert cache.add("k", 3) is True
    assert cache.get("k") == 3


def test_clear(clock):
    cache = TTLCache(maxsize=4, ttl=10, timer=clock)
    cache.set("k", 1)
    cache.clear()
    assert len(cache) == 0


def test_rejects_non_positive_maxsize():
    with pytest.raises(ValueError):
        TTLCache(maxsize=0)
