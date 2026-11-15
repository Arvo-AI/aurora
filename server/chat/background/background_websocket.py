"""Minimal websocket interface for background chat execution."""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BackgroundWebSocket:
    """Minimal websocket for background chats - discards messages instead of streaming.
    
    Background chats run without a real WebSocket connection (e.g., triggered by webhooks).
    This class implements the minimal interface needed by process_workflow_async(),
    allowing the workflow to run while all messages are saved to the database instead
    of being streamed to a frontend.

    An optional ``on_message`` observer receives each frame (the JSON string the
    workflow would have sent) so callers can surface progress elsewhere, e.g. Slack.
    """

    def __init__(self, on_message: Optional[Callable[[Any], None]] = None):
        self._on_message = on_message

    async def send(self, message):
        """Discard the frame, after handing it to the observer if one is attached."""
        if self._on_message is None:
            logger.debug("[BackgroundChat] Message discarded (no active WebSocket)")
            return
        try:
            self._on_message(message)
        except Exception:
            # Progress reporting must never break the workflow that feeds it
            logger.debug("[BackgroundChat] on_message observer failed", exc_info=True)
