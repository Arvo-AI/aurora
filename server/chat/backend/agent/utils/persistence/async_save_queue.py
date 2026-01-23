"""Async queue for non-blocking database saves."""

import asyncio
import logging
import time
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
import json

logger = logging.getLogger(__name__)


@dataclass
class SaveTask:
    """Represents a save task in the queue."""
    session_id: str
    user_id: str
    messages: List[Dict[str, Any]]
    tool_capture: Optional[List[Any]] = None
    timestamp: float = field(default_factory=time.time)
    retry_count: int = 0


class AsyncSaveQueue:
    """Manages asynchronous saving of context history."""
    
    def __init__(self, save_function: Callable, max_queue_size: int = 100):
        """Initialize the async save queue.
        
        Args:
            save_function: The synchronous save function to call
            max_queue_size: Maximum number of items in queue
        """
        self.save_function = save_function
        self.max_queue_size = max_queue_size
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.worker_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Deduplication tracking
        self._pending_saves: Dict[str, SaveTask] = {}
        
    async def start(self):
        """Start the async save worker."""
        if not self._running:
            self._running = True
            self.worker_task = asyncio.create_task(self._save_worker())
            logger.info("✓ Async save queue started")
    
    async def stop(self):
        """Stop the async save worker gracefully."""
        self._running = False
        if self.worker_task:
            await self.queue.put(None)  # Sentinel to stop worker
            await self.worker_task
            logger.info("✓ Async save queue stopped")
    
    async def enqueue_save(self, session_id: str, user_id: str, 
                          messages: List[Dict[str, Any]], 
                          tool_capture: Optional[List[Any]] = None) -> bool:
        """Add a save task to the queue (non-blocking).
        
        Returns:
            bool: True if enqueued, False if queue full or duplicate
        """
        try:
            # Check for duplicate pending save
            if session_id in self._pending_saves:
                # Update the existing pending save with latest data
                self._pending_saves[session_id].messages = messages
                self._pending_saves[session_id].tool_capture = tool_capture
                logger.debug(f"Updated pending save for session {session_id}")
                return True
            
            task = SaveTask(session_id, user_id, messages, tool_capture)
            
            # Try to add to queue without blocking
            self.queue.put_nowait(task)
            self._pending_saves[session_id] = task
            
            logger.debug(f"Enqueued save for session {session_id} (queue size: {self.queue.qsize()})")
            return True
            
        except asyncio.QueueFull:
            logger.warning(f"Save queue full, dropping save for session {session_id}")
            return False
        except Exception as e:
            logger.error(f"Error enqueueing save: {e}")
            return False
    
    async def _save_worker(self):
        """Background worker that processes save tasks."""
        logger.info("Save worker started")
        
        while self._running:
            try:
                # Wait for task with timeout
                task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                
                if task is None:  # Sentinel value to stop
                    break
                    
                # Remove from pending
                self._pending_saves.pop(task.session_id, None)
                
                # Execute save in thread pool to avoid blocking
                await asyncio.get_event_loop().run_in_executor(
                    None, 
                    self._execute_save,
                    task
                )
                
            except asyncio.TimeoutError:
                continue  # Check if still running
            except Exception as e:
                logger.error(f"Save worker error: {e}")
                await asyncio.sleep(1)  # Brief pause on error
        
        logger.info("Save worker stopped")
    
    def _execute_save(self, task: SaveTask):
        """Execute the actual save operation."""
        try:
            start_time = time.time()
            
            # Call the original save function
            success = self.save_function(
                task.session_id,
                task.user_id,
                task.messages,
                task.tool_capture
            )
            
            elapsed = time.time() - start_time
            logger.debug(f"Save completed for session {task.session_id} in {elapsed:.2f}s")
            
            if not success and task.retry_count < 2:
                # Retry failed saves
                task.retry_count += 1
                try:
                    self.queue.put_nowait(task)
                    logger.debug(f"Retrying save for session {task.session_id}")
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Save execution error for session {task.session_id}: {e}")
