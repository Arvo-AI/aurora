"""Shared setup for the RabbitMQ route and tool tests."""

from unittest.mock import MagicMock

import utils.cache.redis_client as redis_client

# The tool tests import chat.background, which builds a Redis client at import
# and raises when Redis is unreachable. Stub it so this folder runs on its own.
redis_client.get_redis_client = MagicMock
