"""Optimized context manager with caching and async saves."""

import asyncio
import json
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
            # Keep a reference so the task is not GC'd before start() completes.
            self._queue_start_task = asyncio.create_task(self.async_queue.start())
        except RuntimeError:
            # No event loop running yet
            logger.debug("Event loop not available for async queue")
    
    @classmethod
    def save_context_history(cls, session_id: str, user_id: str,
                           messages: List[Dict[str, Any]],
                           tool_capture: Optional[List[Any]] = None) -> bool:
        """Save LLM context. Runs synchronously.

        The previous async-queue path was removed because it raced with
        asyncio.run() teardown in Celery tasks and silently dropped saves.

        The previous MD5 last-message dedup guard was also removed: it caused
        real-world data loss when two consecutive turns happened to end with
        the same trailing assistant text (e.g. "Done."). Idempotency belongs
        at the chat_events terminal-UNIQUE layer, not on a content hash.
        """
        instance = cls._get_instance()
        if not session_id or not user_id:
            return False
        try:
            return instance._execute_actual_save(
                session_id, user_id, messages, tool_capture
            )
        except Exception as e:
            logger.error(f"Optimized save error: {e}")
            return False
    
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
        from datetime import datetime
        from utils.db.connection_pool import db_pool
        
        try:
            logger.info(f"Saving context for session {session_id}: {len(messages)} messages")
            
            processed_messages = self._apply_summarization(messages, tool_capture)
            serialized_messages = self._serialize_messages(processed_messages)
            
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                from utils.auth.stateless_auth import set_rls_context
                if not set_rls_context(cursor, conn, user_id, log_prefix="[ContextManager]"):
                    return False
                
                result = self._upsert_session(
                    cursor, conn, session_id, user_id,
                    json.dumps(serialized_messages), datetime.now(),
                )
                if result is not None:
                    return result

                conn.commit()
                logger.info(f"Saved complete LLM context history for session {session_id} with {len(messages)} messages")
                return True
                
        except Exception as e:
            logger.error(f"Error saving LLM context history: {e}")
            return False

    def _apply_summarization(self, messages, tool_capture):
        """Replace tool messages with their summarized versions when available."""
        processed = []
        for msg in messages:
            if (hasattr(msg, 'tool_call_id') and 
                tool_capture and 
                hasattr(tool_capture, 'summarized_tool_results') and
                msg.tool_call_id in tool_capture.summarized_tool_results):
                
                summarized_data = tool_capture.summarized_tool_results[msg.tool_call_id]
                logger.info(f"Using summarized content for tool_call_id {msg.tool_call_id} in context storage")
                from langchain_core.messages import ToolMessage
                processed.append(ToolMessage(
                    content=summarized_data['summarized_output'],
                    tool_call_id=msg.tool_call_id,
                ))
            else:
                processed.append(msg)
        return processed

    def _serialize_messages(self, processed_messages):
        """Serialize messages, using cache when possible."""
        cached_serialized = self.cache.get_serialized(processed_messages)
        if cached_serialized:
            logger.debug(f"Using cached serialization for {len(processed_messages)} messages")
            return json.loads(cached_serialized)
        serialized = [LLMContextManager.serialize_message(msg) for msg in processed_messages]
        self.cache.set_serialized(processed_messages, json.dumps(serialized))
        return serialized

    @staticmethod
    def _upsert_session(cursor, conn, session_id, user_id, context_json, now):
        """Try UPDATE, then INSERT if the session doesn't exist. Returns bool or None (updated OK, continue)."""
        cursor.execute("""
            UPDATE chat_sessions 
            SET llm_context_history = %s, updated_at = %s
            WHERE id = %s AND user_id = %s
        """, (context_json, now, session_id, user_id))

        if cursor.rowcount > 0:
            return None

        cursor.execute("""
            SELECT COUNT(*) FROM chat_sessions 
            WHERE id = %s AND user_id = %s AND is_active = true
        """, (session_id, user_id))
        if cursor.fetchone()[0] > 0:
            logger.error(f"Failed to update context for existing session {session_id}")
            return False

        try:
            logger.info(f"Session {session_id} not found - creating it automatically")
            cursor.execute("""
                INSERT INTO chat_sessions (id, user_id, title, messages, ui_state, llm_context_history, created_at, updated_at, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (session_id, user_id, "New Chat", json.dumps([]), json.dumps({}), context_json, now, now, True))
            conn.commit()
            logger.info(f"Auto-created session {session_id} and saved context")
            return True
        except Exception as create_error:
            logger.error(f"Failed to auto-create session {session_id}: {create_error}")
            return False
    
    @classmethod
    async def flush_session(cls, session_id: str) -> bool:
        """Flush any pending async save for a session so its context is in the DB."""
        instance = cls._get_instance()
        if hasattr(instance, 'async_queue'):
            return await instance.async_queue.flush_session(session_id)
        return True

    @classmethod
    async def cleanup(cls) -> None:
        """Cleanup resources on shutdown. Must be awaited from an async context."""
        if hasattr(cls, '_instance'):
            await cls._instance.async_queue.stop()
