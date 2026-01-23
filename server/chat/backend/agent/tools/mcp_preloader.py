"""
MCP Tools Preloader Service
Preloads and refreshes MCP tools in the background to eliminate initialization delays.
"""

import asyncio
import logging
import time
from typing import Dict, Set, Optional
from datetime import datetime, timedelta
import threading
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MCP Preloader Configuration
MCP_REFRESH_INTERVAL = 300  # 5 minutes - how often to refresh MCP tools cache
MCP_PRELOAD_ON_AUTH = False  # Whether to preload MCP tools immediately on user authentication
MCP_MAX_IDLE_TIME = 3600  # 1 hour - how long a user can be idle before removing from preload list

class MCPPreloader:
    """Background service that preloads and refreshes MCP tools for active users."""
    
    def __init__(self):
        self.active_users: Set[str] = set()
        self.last_activity: Dict[str, float] = {}
        self.refresh_interval = MCP_REFRESH_INTERVAL
        self.preload_on_auth = MCP_PRELOAD_ON_AUTH
        self.max_idle_time = MCP_MAX_IDLE_TIME
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mcp-preloader")
        self._background_task = None
        
    def start(self):
        """Start the background preloader service."""
        if self._running:
            logger.info("MCP Preloader already running")
            return
            
        self._running = True
        self._background_task = threading.Thread(target=self._run_background_loop, daemon=True)
        self._background_task.start()
        logger.info("MCP Preloader service started")
        
    def stop(self):
        """Stop the background preloader service."""
        self._running = False
        if self._background_task:
            self._background_task.join(timeout=5)
        self._executor.shutdown(wait=False)
        logger.info("MCP Preloader service stopped")
        
    def add_user(self, user_id: str):
        """Add a user to the preload list (called on login/auth)."""
        self.active_users.add(user_id)
        self.last_activity[user_id] = time.time()
        
        if self.preload_on_auth:
            # Immediately trigger preload for this user
            self._executor.submit(self._preload_for_user, user_id)
            logger.info(f"Triggered immediate MCP preload for user {user_id}")
            
    def update_activity(self, user_id: str):
        """Update last activity time for a user (called on each request)."""
        self.last_activity[user_id] = time.time()
        if user_id not in self.active_users:
            self.add_user(user_id)
            
    def remove_user(self, user_id: str):
        """Remove a user from the preload list."""
        self.active_users.discard(user_id)
        self.last_activity.pop(user_id, None)
        logger.info(f"Removed user {user_id} from MCP preload list")
        
    def _run_background_loop(self):
        """Main background loop that refreshes MCP tools."""
        logger.info("Starting MCP background refresh loop")
        
        while self._running:
            try:
                current_time = time.time()
                
                # Clean up idle users
                idle_users = []
                for user_id, last_time in list(self.last_activity.items()):
                    if current_time - last_time > self.max_idle_time:
                        idle_users.append(user_id)
                        
                for user_id in idle_users:
                    self.remove_user(user_id)
                    logger.info(f"Removed idle user {user_id} (inactive for {self.max_idle_time}s)")
                
                # Refresh MCP tools for active users
                for user_id in list(self.active_users):
                    try:
                        # Check if cache needs refresh
                        if self._should_refresh(user_id):
                            self._executor.submit(self._preload_for_user, user_id)
                    except Exception as e:
                        logger.error(f"Error checking refresh for user {user_id}: {e}")
                
                # Sleep before next iteration
                time.sleep(min(60, self.refresh_interval / 5))  # Check more frequently than refresh interval
                
            except Exception as e:
                logger.error(f"Error in MCP background loop: {e}")
                time.sleep(10)  # Brief pause before retrying
                
    def _should_refresh(self, user_id: str) -> bool:
        """Check if a user's MCP tools cache needs refresh."""
        try:
            from chat.backend.agent.tools.mcp_tools import _mcp_tools_cache_expiry, MCP_TOOLS_CACHE_DURATION
            
            current_time = time.time()
            
            # If not cached yet, needs loading
            if user_id not in _mcp_tools_cache_expiry:
                return True
                
            # If cache is about to expire (within 1 minute), refresh it
            time_until_expiry = _mcp_tools_cache_expiry[user_id] - current_time
            if time_until_expiry < 60:  # Refresh if less than 1 minute remaining
                logger.info(f"MCP cache for user {user_id} expiring in {time_until_expiry:.0f}s, refreshing")
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking cache expiry for user {user_id}: {e}")
            return False
            
    def _preload_for_user(self, user_id: str):
        """Preload MCP tools for a specific user."""
        try:
            logger.info(f"Starting MCP preload for user {user_id}")
            
            # Run the async function in a new event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                from chat.backend.agent.tools.cloud_tools import get_real_mcp_tools_for_user
                
                # This will populate the cache
                tools = loop.run_until_complete(get_real_mcp_tools_for_user(user_id))
                
                if tools:
                    logger.info(f"Successfully preloaded {len(tools)} MCP tools for user {user_id}")
                else:
                    logger.info(f"No MCP tools to preload for user {user_id}")
                    
            finally:
                loop.close()
                
        except Exception as e:
            logger.error(f"Error preloading MCP tools for user {user_id}: {e}")

# Global preloader instance
_preloader: Optional[MCPPreloader] = None

def get_preloader() -> MCPPreloader:
    """Get or create the global MCP preloader instance."""
    global _preloader
    if _preloader is None:
        _preloader = MCPPreloader()
    return _preloader

def start_mcp_preloader():
    """Start the MCP preloader service."""
    preloader = get_preloader()
    preloader.start()
    return preloader

def stop_mcp_preloader():
    """Stop the MCP preloader service."""
    global _preloader
    if _preloader:
        _preloader.stop()
        _preloader = None

def preload_user_tools(user_id: str):
    """Trigger MCP tools preload for a specific user (call on login/auth)."""
    preloader = get_preloader()
    preloader.add_user(user_id)

def update_user_activity(user_id: str):
    """Update user activity timestamp (call on each request)."""
    preloader = get_preloader()
    preloader.update_activity(user_id)
