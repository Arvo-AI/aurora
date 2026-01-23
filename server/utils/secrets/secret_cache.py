"""Redis-based secret caching, shared across all containers."""
import logging
from typing import Optional
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

DEFAULT_SECRET_CACHE_TTL_SECONDS = 300  # 5 minutes - TTL for cached secrets


def get_cached_secret(secret_name: str) -> Optional[str]:
    """Retrieve a secret from Redis cache."""
    redis_client = get_redis_client()
    if not redis_client:
        logger.debug("Redis unavailable, skipping cache lookup")
        return None
    
    try:
        cache_key = f"secret:{secret_name}"
        value = redis_client.get(cache_key)
        if value:
            logger.info(f"  Cache HIT for secret: {secret_name[:60]}...")
        else:
            logger.debug(f"Cache MISS for secret: {secret_name[:60]}...")
        return value
    except Exception as e:
        logger.warning(f"Redis get failed: {e}")
        return None


def update_secret_cache(secret_name: str, secret_value: str, ttl_seconds: Optional[int] = None):
    """Store a secret in Redis cache with TTL."""
    redis_client = get_redis_client()
    if not redis_client:
        logger.debug("Redis unavailable, skipping cache update")
        return
    
    try:
        if ttl_seconds is None:
            ttl_seconds = DEFAULT_SECRET_CACHE_TTL_SECONDS
        
        cache_key = f"secret:{secret_name}"
        redis_client.setex(cache_key, ttl_seconds, secret_value)
        logger.info(f"  Cached secret '{secret_name[:60]}...' with TTL {ttl_seconds}s")
    except Exception as e:
        logger.warning(f"Redis set failed: {e}")


def clear_secret_cache(secret_name: Optional[str] = None):
    """Delete a secret from Redis cache."""
    redis_client = get_redis_client()
    if not redis_client:
        logger.warning("Redis unavailable, cannot clear cache")
        return
    
    try:
        if secret_name:
            cache_key = f"secret:{secret_name}"
            deleted = redis_client.delete(cache_key)
            if deleted:
                logger.info(f"  Cleared cache for secret: {secret_name[:60]}...")
            else:
                logger.debug(f"Secret not in cache (already cleared or never cached)")
        else:
            keys = redis_client.keys("secret:*")
            if keys:
                redis_client.delete(*keys)
                logger.info(f"  Cleared all secret cache entries ({len(keys)} keys)")
            else:
                logger.debug("No secrets to clear from cache")
    except Exception as e:
        logger.error(f"Redis delete failed: {e}")
