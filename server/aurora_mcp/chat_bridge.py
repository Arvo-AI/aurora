"""Adapter that exposes Aurora's general chat agent over MCP.

Mirrors the ask_incident polling pattern. v1 uses poll-with-timeout so it
works across all MCP clients regardless of progress-notification support.

Backed by two Flask routes (added to server/routes/chat_routes.py):
  POST /chat_api/sessions                         -> create empty session
  POST /chat_api/sessions/<id>/messages           -> dispatch agent
  GET  /chat_api/sessions/<id>/messages?after=N   -> poll new messages

The HTTP routes are the canonical entry to the agent; this module is purely
a translation layer between the MCP signature and those routes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Poll budget. Total wall-time is _POLL_TOTAL_SECONDS; the interval starts at
# _POLL_INTERVAL_INITIAL and doubles up to _POLL_INTERVAL_MAX between attempts.
# Backoff keeps fast responses snappy (~1s for a quick reply) while capping
# slow-tail roundtrips at ~9 instead of 22.
_POLL_TOTAL_SECONDS = 45.0
_POLL_INTERVAL_INITIAL = 1.0
_POLL_INTERVAL_MAX = 4.0

_TERMINAL_OK = frozenset({"complete", "completed"})
_TERMINAL_ERR = frozenset({"error", "cancelled", "failed"})

# Canonical assistant-message sender tag in chat_sessions.messages is "bot"
# (see DB). Accept "aurora" too in case the schema ever shifts.
_ASSISTANT_SENDERS = frozenset({"bot", "aurora"})


async def chat_with_aurora(
    api_call: Callable[..., Awaitable[Dict[str, Any]]],
    *,
    message: str = "",
    session_id: Optional[str] = None,
    mode: str = "chat",
    poll_only: bool = False,
) -> Dict[str, Any]:
    """Send `message` to Aurora's chat agent and return its response.

    Args:
        api_call: bound `_api` proxy from mcp_server.py (forwards user identity).
        message: user message text. Ignored when poll_only=True.
        session_id: continue an existing session, or None to start a new one.
        mode: "chat" (default) or "rca" — passed to the backend agent.
        poll_only: when True, skip create+post and just poll session_id for new
            assistant messages. Use to resume a still-running session without
            sending a new turn.
    """
    if mode not in ("chat", "rca"):
        return {"error": "mode must be 'chat' or 'rca'"}
    if poll_only and not session_id:
        return {"error": "session_id is required when poll_only=True"}
    if not poll_only and not isinstance(message, str):
        return {"error": "message must be a string"}

    sid = session_id
    last_seq = 0

    if not poll_only:
        if not sid:
            created = await api_call(
                "POST", "/chat_api/sessions",
                body={"title": "MCP chat", "ui_state": {"isMCP": True}},
                timeout=30,
            )
            sid = created.get("id")
            if not sid:
                return {"error": "Failed to create chat session", "raw": created}

        if message:
            posted = await api_call(
                "POST", f"/chat_api/sessions/{sid}/messages",
                body={"message": message, "mode": mode}, timeout=30,
            )
            last_seq = int(posted.get("seq") or 0)

    elapsed = 0.0
    interval = _POLL_INTERVAL_INITIAL
    latest_partial: Optional[str] = None

    while elapsed < _POLL_TOTAL_SECONDS:
        await asyncio.sleep(interval)
        elapsed += interval
        interval = min(interval * 2, _POLL_INTERVAL_MAX)

        page = await api_call(
            "GET", f"/chat_api/sessions/{sid}/messages",
            params={"after": last_seq}, timeout=15,
        )
        msgs: List[Dict[str, Any]] = page.get("messages") or []
        status = page.get("status", "unknown")

        if msgs:
            last_seq = int(page.get("seq") or last_seq + len(msgs))
            for m in reversed(msgs):
                if m.get("sender") in _ASSISTANT_SENDERS:
                    latest_partial = m.get("text") or latest_partial
                    break

        if status in _TERMINAL_OK:
            return {
                "session_id": sid,
                "status": "complete",
                "response": latest_partial or "",
                "citations": page.get("citations", []),
            }
        if status in _TERMINAL_ERR:
            return {
                "session_id": sid,
                "status": status,
                "error": page.get("error") or "Chat session ended unsuccessfully",
            }

    return {
        "session_id": sid,
        "status": "in_progress",
        "partial": latest_partial or "",
        "hint": (
            "Aurora is still working. Call chat_with_aurora again with "
            "session_id and poll_only=True to continue polling."
        ),
    }
