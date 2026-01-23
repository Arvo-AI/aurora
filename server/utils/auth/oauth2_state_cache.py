"""
OAuth2 State Cache - Redis-based distributed storage for OAuth2 state tokens.

This module provides Redis-backed state token storage for OAuth2 flows to support:
- Load balancer compatibility (distributed state across multiple servers)
- Server restart resilience (persistent storage)
- Automatic expiration (Redis TTL)
- Atomic operations (replay protection)

Requirements:
- Redis 6.2+ (for GETDEL atomic operation, fallback to GET+DEL for older versions)
- REDIS_URL environment variable pointing to Redis instance

Security Features:
- Single-use tokens (atomic get-and-delete)
- Automatic expiration (30-minute TTL)
- Thread-safe operations
- No in-memory fallback (ensures consistent behavior across dev/prod)

Example:
    REDIS_URL=redis://localhost:6379/0
    docker-compose up redis
"""

import json
import logging
import os
import time
from typing import Dict, Optional, Any

import redis

logger = logging.getLogger(__name__)

# Redis configuration - REQUIRED (no default to force explicit configuration)
REDIS_URL = os.getenv('REDIS_URL')
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL environment variable is required for OAuth2 state cache. "
        "Set REDIS_URL in your .env file (e.g., REDIS_URL=redis://localhost:6379/0) "
        "or start Redis: docker-compose up redis"
    )

# Initialize Redis client
try:
    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5
    )
    # Test connection
    redis_client.ping()
    logger.info(f"Redis connected successfully: {REDIS_URL}")
except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
    logger.error(f"CRITICAL: Redis connection failed: {e}")
    logger.error(f"Redis is REQUIRED for OAuth2 state cache")
    logger.error(f"Please ensure Redis is running at: {REDIS_URL}")
    logger.error(f"To start Redis: docker-compose up redis")
    raise RuntimeError(
        f"Redis connection failed. OAuth2 requires Redis for secure state storage. "
        f"Ensure Redis is running at {REDIS_URL}"
    ) from e


def store_oauth2_state(
    state: str,
    user_id: str,
    endpoint: str,
    project_id: Optional[str] = None,
    code_verifier: Optional[str] = None
) -> None:
    """
    Store OAuth2 state for authorization flow in Redis.

    Args:
        state: CSRF state token (32-byte random string)
        user_id: User ID from authentication
        endpoint: OAuth2 endpoint (ovh-eu, ovh-us, ovh-ca)
        project_id: Optional project ID to auto-select after auth
        code_verifier: PKCE code verifier for security

    Raises:
        redis.exceptions.RedisError: If Redis operation fails

    Security:
        - Tokens expire after 30 minutes (TTL)
        - Atomic operations prevent race conditions
        - Single-use (deleted on retrieval)

    Example:
        store_oauth2_state(
            state="TmAcSVNJIUCW...",
            user_id="user_abc123",
            endpoint="ovh-eu",
            code_verifier="PKCEverifier..."
        )
    """
    data = {
        'user_id': user_id,
        'endpoint': endpoint,
        'timestamp': str(int(time.time())),
    }

    if code_verifier:
        data['code_verifier'] = code_verifier

    if project_id:
        data['project_id'] = project_id

    key = f"oauth2:state:{state}"
    
    try:
        redis_client.setex(
            key,
            1800,  # 30 minutes TTL
            json.dumps(data)
        )
        logger.debug(f"Stored OAuth2 state in Redis: {state[:10]}... (expires in 30min)")
    except redis.exceptions.RedisError as e:
        logger.error(f"Failed to store OAuth2 state in Redis: {e}")
        raise


def retrieve_oauth2_state(state: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve and delete OAuth2 state (atomic single-use).

    Args:
        state: CSRF state token to retrieve

    Returns:
        State data dict if valid, None if not found or expired

    Security:
        - Atomic get-and-delete prevents replay attacks
        - State tokens are single-use only
        - Expiration enforced by Redis TTL

    Example:
        state_data = retrieve_oauth2_state("TmAcSVNJIUCW...")
        if state_data:
            user_id = state_data['user_id']
            endpoint = state_data['endpoint']
    """
    key = f"oauth2:state:{state}"

    try:
        # Try GETDEL (Redis 6.2+) - atomic get-and-delete
        value = redis_client.getdel(key)
    except redis.exceptions.ResponseError:
        # Fallback for Redis < 6.2: use pipeline for atomicity
        logger.debug("GETDEL not available, using GET+DEL pipeline")
        pipe = redis_client.pipeline()
        pipe.get(key)
        pipe.delete(key)
        results = pipe.execute()
        value = results[0]

    if not value:
        logger.warning(f"State token not found in Redis: {state[:10]}... (expired or invalid)")
        return None

    try:
        data = json.loads(value)
        logger.debug(f"Retrieved OAuth2 state from Redis: {state[:10]}...")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode OAuth2 state data: {e}")
        return None


def clear_oauth2_states() -> None:
    """
    Clear all OAuth2 state tokens.

    Warning:
        This will invalidate ALL in-flight OAuth2 flows!
        Use with caution, typically only for testing or emergency cleanup.

    Example:
        # Emergency: Clear all OAuth2 states
        clear_oauth2_states()
    """
    try:
        keys = redis_client.keys("oauth2:state:*")
        if keys:
            redis_client.delete(*keys)
            logger.info(f"Cleared {len(keys)} OAuth2 states from Redis")
        else:
            logger.debug("No OAuth2 states to clear")
    except redis.exceptions.RedisError as e:
        logger.error(f"Failed to clear OAuth2 states: {e}")
        raise
