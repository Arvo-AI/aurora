"""Shared setup for the chat tests."""

from unittest.mock import MagicMock

import utils.cache.redis_client as redis_client

# chat.background.task builds a Redis client at import and raises when Redis is
# unreachable. CI does not install redis (tests/conftest.py stubs the package);
# with the real package and no server running, as on a laptop, collection of
# anything importing chat.background would fail. Match CI here.
redis_client.get_redis_client = MagicMock
