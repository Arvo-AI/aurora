"""Redis-based caching for message serialization."""

import json
import logging
import hashlib
import redis
from typing import Optional, Dict, Any, List
import os

logger = logging.getLogger(__name__)


class RedisCache:
    """Handles Redis caching for serialized messages and deduplication."""
    
    def __init__(self):
        """Initialize Redis connection."""
        self.redis_client = None
        self._connect()
        
    def _connect(self):
        """Connect to Redis server."""
        try:
            redis_url = os.getenv('REDIS_URL')
            if not redis_url:
                raise ValueError("REDIS_URL environment variable is not set")
            self.redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            # Test connection
            self.redis_client.ping()
            logger.info("✓ Connected to Redis for caching")
        except Exception as e:
            logger.warning(f"Redis connection failed, caching disabled: {e}")
            self.redis_client = None
    
    def get_cache_key(self, prefix: str, content: str) -> str:
        """Generate a cache key from content hash."""
        content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        return f"aurora:{prefix}:{content_hash}"
    
    def get_serialized(self, messages: List[Any]) -> Optional[str]:
        """Get cached serialized messages."""
        if not self.redis_client:
            return None
            
        try:
            # Create cache key from message content
            if not messages:
                return "[]"
            
            # Use last message and count for cache key
            last_message_content = getattr(messages[-1], 'content', str(messages[-1]))
            key_content = f"{len(messages)}:{str(last_message_content)[:100]}"
            cache_key = self.get_cache_key("serialized", key_content)
            
            cached = self.redis_client.get(cache_key)
            if cached:
                logger.debug(f"Cache hit for {len(messages)} messages")
            return cached
            
        except Exception as e:
            logger.debug(f"Cache get error: {e}")
            return None
    
    def set_serialized(self, messages: List[Any], serialized: str, ttl: int = 300):
        """Cache serialized messages with TTL."""
        if not self.redis_client:
            return
            
        try:
            if not messages:
                return
                
            last_message_content = getattr(messages[-1], 'content', str(messages[-1]))
            key_content = f"{len(messages)}:{str(last_message_content)[:100]}"
            cache_key = self.get_cache_key("serialized", key_content)
            
            self.redis_client.setex(cache_key, ttl, serialized)
            logger.debug(f"Cached serialization for {len(messages)} messages")
            
        except Exception as e:
            logger.debug(f"Cache set error: {e}")
    
    def check_duplicate_save(self, session_id: str, content_hash: str) -> bool:
        """Check if this exact content was recently saved."""
        if not self.redis_client:
            return False
            
        try:
            cache_key = f"aurora:save_hash:{session_id}"
            last_hash = self.redis_client.get(cache_key)
            
            if last_hash == content_hash:
                logger.debug(f"Duplicate save detected for session {session_id}")
                return True
                
            # Update with new hash (5 minute TTL)
            self.redis_client.setex(cache_key, 300, content_hash)
            return False
            
        except Exception as e:
            logger.debug(f"Duplicate check error: {e}")
            return False
