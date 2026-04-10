"""Centralized Redis client with health checks, reusable across all modules."""
import os
import logging
from typing import Optional
import redis

logger = logging.getLogger(__name__)

_redis_client: Optional[redis.Redis] = None


def get_redis_ssl_kwargs() -> dict:
    """Build SSL kwargs for redis.from_url() based on REDIS_SSL_* env vars."""
    kwargs = {}
    ca_certs = os.getenv("REDIS_SSL_CA_CERTS")
    if ca_certs:
        kwargs["ssl_ca_certs"] = ca_certs
    cert_reqs = os.getenv("REDIS_SSL_CERT_REQS")
    if cert_reqs:
        kwargs["ssl_cert_reqs"] = cert_reqs
    return kwargs


def get_redis_client() -> Optional[redis.Redis]:
    """Get a healthy Redis client with automatic reconnection."""
    global _redis_client
    
    try:
        if _redis_client is not None:
            try:
                _redis_client.ping()
                return _redis_client
            except Exception:
                logger.warning("Redis client unhealthy, reconnecting...")
                _redis_client = None
        
        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        _redis_client = redis.from_url(url, decode_responses=True, **get_redis_ssl_kwargs())
        
        _redis_client.ping()
        logger.debug("Redis client connected")
        return _redis_client
        
    except Exception as e:
        logger.error(f"Redis unavailable: {e}")
        return None

