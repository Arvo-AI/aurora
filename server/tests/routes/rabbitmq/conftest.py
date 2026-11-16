"""Shared setup for the RabbitMQ route and tool tests."""

import contextlib
from unittest.mock import MagicMock

import pytest

import utils.cache.redis_client as redis_client


@pytest.fixture(autouse=True)
def _no_live_redis(monkeypatch):
    """Keep these tests off a real Redis, and undo it after each test.

    The tool tests import chat.background, which builds a Redis client at import
    and raises when none is reachable. The route tests take the broker-list lock.
    monkeypatch restores get_redis_client when each test ends, so tests outside
    this folder still get the real function.
    """
    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    yield


@pytest.fixture
def no_lock(monkeypatch):
    """Route tests exercise the handlers, not the lock."""
    from routes.rabbitmq import rabbitmq_routes

    monkeypatch.setattr(rabbitmq_routes, "accounts_lock", lambda user_id: contextlib.nullcontext())
