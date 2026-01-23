"""Centralized Redis client with health checks, reusable across all modules."""
import os
import logging
from typing import Optional
import redis

logger = logging.getLogger(__name__)

_redis_client: Optional[redis.Redis] = None


def get_redis_client() -> Optional[redis.Redis]:
    """Get a healthy Redis client with automatic reconnection."""
    global _redis_client
    
    try:
        # Health check on existing client
        if _redis_client is not None:
            try:
                _redis_client.ping()
                return _redis_client
            except Exception:
                logger.warning("Redis client unhealthy, reconnecting...")
                _redis_client = None
        
        # Create new client
        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        _redis_client = redis.from_url(url, decode_responses=True)
        
        _redis_client.ping()
        logger.debug("Redis client connected")
        return _redis_client
        
    except Exception as e:
        logger.error(f"Redis unavailable: {e}")
        return None

