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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Poll budget. Total wall-time is _POLL_TOTAL_SECONDS; the interval starts at
# _POLL_INTERVAL_INITIAL and doubles up to _POLL_INTERVAL_MAX between attempts.
# Backoff keeps fast responses snappy (~1s for a quick reply) while capping
# slow-tail roundtrips at ~9 instead of 22.
_POLL_TOTAL_SECONDS = 45.0
_POLL_INTERVAL_INITIAL = 1.0
_POLL_INTERVAL_MAX = 4.0
_POLL_REQUEST_TIMEOUT = 15.0

_TERMINAL_OK = frozenset({"complete", "completed"})
_TERMINAL_ERR = frozenset({"error", "cancelled", "failed"})

# Canonical assistant-message sender tag in chat_sessions.messages is "bot"
# (see DB). Accept "aurora" too in case the schema ever shifts.
_ASSISTANT_SENDERS = frozenset({"bot", "aurora"})

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]


def _validate_inputs(message: Any, session_id: Optional[str], mode: str, poll_only: bool) -> Optional[Dict[str, Any]]:
    if mode not in ("chat", "rca"):
        return {"error": "mode must be 'chat' or 'rca'"}
    if poll_only and not session_id:
        return {"error": "session_id is required when poll_only=True"}
    if not poll_only and not isinstance(message, str):
        return {"error": "message must be a string"}
    return None


async def _create_session(api_call: ApiCall) -> Optional[str]:
    created = await api_call(
        "POST", "/chat_api/sessions",
        body={"title": "MCP chat", "ui_state": {"isMCP": True}},
    )
    return created.get("id") if isinstance(created, dict) else None


async def _post_message(api_call: ApiCall, sid: str, message: str, mode: str) -> Optional[int]:
    """Post a message; return the user-message seq, or None if the response
    lacks a usable seq.

    Returning None (not 0) on failure matters because seq=0 would cause the
    subsequent poll to treat every prior message as "new" and surface a stale
    assistant reply from before this turn.
    """
    posted = await api_call(
        "POST", f"/chat_api/sessions/{sid}/messages",
        body={"message": message, "mode": mode},
    )
    if not isinstance(posted, dict):
        return None
    seq = posted.get("seq")
    if isinstance(seq, int) and seq >= 0:
        return seq
    try:
        return int(seq) if seq is not None else None
    except (TypeError, ValueError):
        return None


def _latest_assistant_text(msgs: List[Dict[str, Any]], fallback: Optional[str]) -> Optional[str]:
    for m in reversed(msgs):
        if m.get("sender") in _ASSISTANT_SENDERS:
            return m.get("text") or fallback
    return fallback


def _terminal_result(
    status: str, sid: Optional[str], page: Dict[str, Any], latest_partial: Optional[str]
) -> Optional[Dict[str, Any]]:
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
    return None


async def _poll_once(
    api_call: ApiCall, sid: str, last_seq: int
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, int]:
    async with asyncio.timeout(_POLL_REQUEST_TIMEOUT):
        page = await api_call(
            "GET", f"/chat_api/sessions/{sid}/messages",
            params={"after": last_seq},
        )
    msgs: List[Dict[str, Any]] = page.get("messages") or []
    status = page.get("status", "unknown")
    if msgs:
        last_seq = int(page.get("seq") or last_seq + len(msgs))
    return page, msgs, status, last_seq


async def chat_with_aurora(
    api_call: ApiCall,
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
    err = _validate_inputs(message, session_id, mode, poll_only)
    if err is not None:
        return err

    sid = session_id
    last_seq = 0

    if not poll_only:
        if not sid:
            sid = await _create_session(api_call)
            if not sid:
                return {"error": "Failed to create chat session"}
        if message:
            posted_seq = await _post_message(api_call, sid, message, mode)
            if posted_seq is None:
                return {"error": "Failed to post chat message", "session_id": sid}
            last_seq = posted_seq

    elapsed = 0.0
    interval = _POLL_INTERVAL_INITIAL
    latest_partial: Optional[str] = None

    while elapsed < _POLL_TOTAL_SECONDS:
        await asyncio.sleep(interval)  # NOSONAR S7484: cross-process HTTP poll, no in-process signal to wait on.
        elapsed += interval
        interval = min(interval * 2, _POLL_INTERVAL_MAX)

        page, msgs, status, last_seq = await _poll_once(api_call, sid, last_seq)
        latest_partial = _latest_assistant_text(msgs, latest_partial)

        terminal = _terminal_result(status, sid, page, latest_partial)
        if terminal is not None:
            return terminal

    return {
        "session_id": sid,
        "status": "in_progress",
        "partial": latest_partial or "",
        "hint": (
            "Aurora is still working. Call chat_with_aurora again with "
            f"session_id='{sid}' and poll_only=True to continue polling. "
            "Reuse this same session_id for any follow-up turn — do not "
            "start a new session."
        ),
    }
