"""Optimized context manager with caching and async saves."""

import asyncio
import json
import hashlib
import logging
from typing import List, Dict, Any, Optional
from .redis_cache import RedisCache
from .async_save_queue import AsyncSaveQueue
from chat.backend.agent.utils.llm_context_manager import LLMContextManager

logger = logging.getLogger(__name__)


class ContextManager:
    """Drop-in replacement for LLMContextManager with performance optimizations."""
    
    def __init__(self):
        """Initialize optimized components."""
        self.cache = RedisCache()
        self.async_queue = AsyncSaveQueue(
            save_function=self._execute_actual_save,  # Use our own save logic
            max_queue_size=100
        )
        
        # Start async queue in background
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self.async_queue.start())
        except RuntimeError:
            # No event loop running yet
            logger.debug("Event loop not available for async queue")
    
    @classmethod
    def save_context_history(cls, session_id: str, user_id: str, 
                           messages: List[Dict[str, Any]], 
                           tool_capture: Optional[List[Any]] = None) -> bool:
        """Optimized save with caching and async execution.
        
        This method replaces the original synchronous save with:
        1. Deduplication checking
        2. Cached serialization
        3. Async non-blocking saves
        """
        instance = cls._get_instance()
        
        try:
            # Quick validation
            if not session_id or not user_id:
                return False
            
            # Check for duplicate save (using Redis)
            if messages:
                # Use message content for hash, not the whole object
                last_message_content = getattr(messages[-1], 'content', str(messages[-1]))
                content_hash = hashlib.md5(
                    str(last_message_content).encode()
                ).hexdigest()[:16]
                
                if instance.cache.check_duplicate_save(session_id, content_hash):
                    logger.debug(f"Skipping duplicate save for session {session_id}")
                    return True
            
            # Try async save first
            if hasattr(instance, 'async_queue'):
                try:
                    loop = asyncio.get_running_loop()
                    # Schedule async save without blocking
                    future = asyncio.create_task(
                        instance.async_queue.enqueue_save(
                            session_id, user_id, messages, tool_capture
                        )
                    )
                    logger.debug(f"Scheduled async save for session {session_id}")
                    return True  # Return immediately, save happens in background
                    
                except RuntimeError:
                    # No event loop, fall back to sync save
                    pass
            
            # Fallback to synchronous save if async not available
            return instance._execute_actual_save(
                session_id, user_id, messages, tool_capture
            )
            
        except Exception as e:
            logger.error(f"Optimized save error: {e}")
            # Fall back to direct save on any error
            return instance._execute_actual_save(
                session_id, user_id, messages, tool_capture
            )
    
    @classmethod
    def get_optimized_serialization(cls, messages: List[Dict[str, Any]]) -> str:
        """Get serialized messages with caching."""
        instance = cls._get_instance()
        
        # Check cache first
        cached = instance.cache.get_serialized(messages)
        if cached:
            return cached
        
        # Serialize (reuse existing serialization logic)
        serialized_messages = [
            LLMContextManager.serialize_message(msg) for msg in messages
        ]
        serialized = json.dumps(serialized_messages)
        
        # Cache for next time
        instance.cache.set_serialized(messages, serialized)
        
        return serialized
    
    @classmethod
    def _get_instance(cls):
        """Get or create singleton instance."""
        if not hasattr(cls, '_instance'):
            cls._instance = cls()
        return cls._instance
    
    def _execute_actual_save(self, session_id: str, user_id: str, 
                           messages: List[Dict[str, Any]], 
                           tool_capture: Optional[List[Any]] = None) -> bool:
        """Execute the actual database save operation (moved from LLMContextManager)."""
        import json
        from datetime import datetime
        from utils.db.connection_pool import db_pool
        
        try:
            logger.info(f"Saving context for session {session_id}: {len(messages)} messages")
            
            # Process messages to use summarized content for context storage to save tokens
            processed_messages = []
            for msg in messages:
                # Check if this is a tool message that has summarized content available
                if (hasattr(msg, 'tool_call_id') and 
                    tool_capture and 
                    hasattr(tool_capture, 'summarized_tool_results') and
                    msg.tool_call_id in tool_capture.summarized_tool_results):
                    
                    # Use summarized content for context storage to save tokens
                    summarized_data = tool_capture.summarized_tool_results[msg.tool_call_id]
                    logger.info(f"Using summarized content for tool_call_id {msg.tool_call_id} in context storage")
                    
                    # Create a copy of the message with summarized content for storage
                    from langchain_core.messages import ToolMessage
                    summarized_msg = ToolMessage(
                        content=summarized_data['summarized_output'],
                        tool_call_id=msg.tool_call_id
                    )
                    processed_messages.append(summarized_msg)
                else:
                    # Use original message
                    processed_messages.append(msg)
            
            # Use cached serialization if available
            cached_serialized = self.cache.get_serialized(processed_messages)
            if cached_serialized:
                serialized_messages = json.loads(cached_serialized)
                logger.debug(f"Using cached serialization for {len(processed_messages)} messages")
            else:
                serialized_messages = [LLMContextManager.serialize_message(msg) for msg in processed_messages]
                # Cache the serialization
                self.cache.set_serialized(processed_messages, json.dumps(serialized_messages))
            
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Try to update existing session first
                cursor.execute("""
                    UPDATE chat_sessions 
                    SET llm_context_history = %s, updated_at = %s
                    WHERE id = %s AND user_id = %s
                """, (json.dumps(serialized_messages), datetime.now(), session_id, user_id))
                
                if cursor.rowcount == 0:
                    # Session doesn't exist, check if it's a valid session that should exist
                    cursor.execute("""
                        SELECT COUNT(*) FROM chat_sessions 
                        WHERE id = %s AND user_id = %s AND is_active = true
                    """, (session_id, user_id))
                    
                    session_exists = cursor.fetchone()[0] > 0
                    
                    if not session_exists:
                        # AUTO-CREATE SESSION: Create the session if it doesn't exist
                        try:
                            logger.info(f"Session {session_id} not found - creating it automatically")
                            cursor.execute("""
                                INSERT INTO chat_sessions (id, user_id, title, messages, ui_state, llm_context_history, created_at, updated_at, is_active)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """, (
                                session_id, 
                                user_id, 
                                "New Chat", 
                                json.dumps([]), 
                                json.dumps({}), 
                                json.dumps(serialized_messages), 
                                datetime.now(), 
                                datetime.now(), 
                                True
                            ))
                            conn.commit()
                            logger.info(f"✓ Auto-created session {session_id} and saved context")
                            return True
                        except Exception as create_error:
                            logger.error(f"Failed to auto-create session {session_id}: {create_error}")
                            return False
                    else:
                        # Session exists but update failed for other reasons
                        logger.error(f"Failed to update context for existing session {session_id}")
                        return False
                
                conn.commit()
                logger.info(f"Saved complete LLM context history for session {session_id} with {len(messages)} messages")
                return True
                
        except Exception as e:
            logger.error(f"Error saving LLM context history: {e}")
            return False
    
    @classmethod
    def cleanup(cls):
        """Cleanup resources on shutdown."""
        if hasattr(cls, '_instance'):
            asyncio.create_task(cls._instance.async_queue.stop())
