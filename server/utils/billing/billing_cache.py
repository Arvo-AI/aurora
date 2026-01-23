"""
API Cost Cache System for Aurora
Provides asynchronous API cost data caching to improve performance.
"""

import asyncio
import logging
import time
from typing import Tuple, Dict

from utils.db.connection_pool import db_pool
from utils.billing.billing_utils import get_api_cost

# Configure logging
logger = logging.getLogger(__name__)

# Global API cost cache to store user costs
# Format: {user_id: {"cost": float, "timestamp": float}}
_api_cost_cache: Dict[str, Dict] = {}
API_COST_CACHE_TTL = 300  # 5 minutes cache TTL

# Track in-progress updates to prevent duplicates
_update_in_progress: Dict[str, bool] = {}


async def update_api_cost_cache_async(user_id: str) -> None:
    """
    Update API cost cache asynchronously in the background.
    This runs without blocking the main request processing.
    Prevents duplicate updates for the same user.
    """
    # Check if update is already in progress for this user
    if _update_in_progress.get(user_id, False):
        logger.debug(f"API cost update already in progress for user {user_id}, skipping duplicate")
        return
    
    # Mark update as in progress
    _update_in_progress[user_id] = True
    
    try:
        # Run the expensive operations in a thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        
        # Get API cost in background thread
        total_cost = await loop.run_in_executor(None, get_api_cost, user_id)
        
        # Update cache
        _api_cost_cache[user_id] = {
            "cost": total_cost,
            "timestamp": time.time()
        }
        
        logger.info(f"Updated API cost cache for user {user_id}: ${total_cost:.2f}")
        
    except Exception as e:
        logger.error(f"Error updating API cost cache for user {user_id}: {e}")
        # Don't update cache on error, keep using old value if available
    finally:
        # Always clear the in-progress flag
        _update_in_progress[user_id] = False


def get_cached_api_cost(user_id: str) -> Tuple[bool, float]:
    """
    Get API cost info from cache if available and not expired.
    
    Args:
        user_id: The user ID to check
        
    Returns:
        Tuple of (is_cached, total_cost)
    """
    if user_id in _api_cost_cache:
        cache_entry = _api_cost_cache[user_id]
        age = time.time() - cache_entry["timestamp"]
        
        if age < API_COST_CACHE_TTL:
            return True, cache_entry["cost"]
    
    return False, 0.0


def is_cache_fresh(user_id: str, freshness_seconds: int = 30) -> bool:
    """
    Check if the cache for a user is fresh enough to skip updates.
    
    Args:
        user_id: The user ID to check
        freshness_seconds: Consider cache fresh if updated within this many seconds
        
    Returns:
        True if cache exists and is fresh, False otherwise
    """
    if user_id in _api_cost_cache:
        cache_entry = _api_cost_cache[user_id]
        age = time.time() - cache_entry["timestamp"]
        return age < freshness_seconds
    
    return False


def clear_user_cache(user_id: str) -> None:
    """
    Clear cache for a specific user.
    
    Args:
        user_id: The user ID to clear cache for
    """
    if user_id in _api_cost_cache:
        del _api_cost_cache[user_id]
        logger.info(f"Cleared API cost cache for user {user_id}")


def clear_all_cache() -> None:
    """
    Clear all API cost cache entries.
    """
    _api_cost_cache.clear()
    logger.info("Cleared all API cost cache entries")


def get_cache_stats() -> Dict:
    """
    Get statistics about the API cost cache.
    
    Returns:
        Dict with cache statistics
    """
    total_entries = len(_api_cost_cache)
    current_time = time.time()
    
    valid_entries = 0
    expired_entries = 0
    
    for user_id, entry in _api_cost_cache.items():
        age = current_time - entry["timestamp"]
        if age < API_COST_CACHE_TTL:
            valid_entries += 1
        else:
            expired_entries += 1
    
    return {
        "total_entries": total_entries,
        "valid_entries": valid_entries,
        "expired_entries": expired_entries,
        "cache_ttl_seconds": API_COST_CACHE_TTL
    } 