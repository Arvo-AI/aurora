"""Minimal websocket interface for background chat execution."""

import logging

logger = logging.getLogger(__name__)


class BackgroundWebSocket:
    """Minimal websocket for background chats - discards messages instead of streaming.
    
    Background chats run without a real WebSocket connection (e.g., triggered by webhooks).
    This class implements the minimal interface needed by process_workflow_async(),
    allowing the workflow to run while all messages are saved to the database instead
    of being streamed to a frontend.
    """
    
    async def send(self, message):
        """No-op send - background chats save to DB, don't stream to frontend."""
        # Log at debug level for troubleshooting if needed
        logger.debug("[BackgroundChat] Message discarded (no active WebSocket)")

