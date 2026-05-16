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
import time
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
    if not poll_only:
        if not isinstance(message, str):
            return {"error": "message must be a string"}
        if not message.strip():
            return {"error": "message must be a non-empty string"}
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


async def _prepare_session(
    api_call: ApiCall,
    sid: Optional[str],
    message: str,
    mode: str,
    poll_only: bool,
) -> Tuple[Optional[str], int, Optional[Dict[str, Any]]]:
    """Create session and post the user message as needed.

    Returns (sid, last_seq, error_envelope). When error_envelope is non-None
    the caller should return it immediately.
    """
    if poll_only:
        return sid, 0, None

    if not sid:
        try:
            sid = await _create_session(api_call)
        except Exception as exc:
            logger.exception("chat_with_aurora: create_session failed")
            return None, 0, {"status": "error", "error": f"Failed to create chat session: {exc}"}
        if not sid:
            return None, 0, {"status": "error", "error": "Failed to create chat session"}

    if not message:
        return sid, 0, None

    try:
        posted_seq = await _post_message(api_call, sid, message, mode)
    except Exception as exc:
        logger.exception("chat_with_aurora: post_message failed (session=%s)", sid)
        return sid, 0, {"session_id": sid, "status": "error",
                        "error": f"Failed to post chat message: {exc}"}
    if posted_seq is None:
        return sid, 0, {"session_id": sid, "status": "error",
                        "error": "Failed to post chat message"}
    return sid, posted_seq, None


async def _poll_step(
    api_call: ApiCall, sid: str, last_seq: int,
) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], str, int]]:
    """One poll iteration. Returns None on transient error so the caller retries."""
    try:
        return await _poll_once(api_call, sid, last_seq)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("chat_with_aurora poll timed out, retrying (session=%s)", sid)
        return None
    except Exception as exc:
        logger.warning(
            "chat_with_aurora poll raised %s, retrying (session=%s): %s",
            type(exc).__name__, sid, exc,
        )
        return None


async def _poll_for_terminal(
    api_call: ApiCall, sid: str, last_seq: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Drive the poll loop until a terminal status or the deadline."""
    deadline = time.monotonic() + _POLL_TOTAL_SECONDS
    interval = _POLL_INTERVAL_INITIAL
    latest_partial: Optional[str] = None

    while time.monotonic() < deadline:
        await asyncio.sleep(interval)  # NOSONAR S7484: cross-process HTTP poll, no in-process signal to wait on.
        interval = min(interval * 2, _POLL_INTERVAL_MAX)

        step = await _poll_step(api_call, sid, last_seq)
        if step is None:
            continue
        page, msgs, status, last_seq = step
        latest_partial = _latest_assistant_text(msgs, latest_partial)
        terminal = _terminal_result(status, sid, page, latest_partial)
        if terminal is not None:
            return terminal, latest_partial

    return None, latest_partial


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

    sid, last_seq, prep_err = await _prepare_session(
        api_call, session_id, message, mode, poll_only,
    )
    if prep_err is not None:
        return prep_err

    terminal, latest_partial = await _poll_for_terminal(api_call, sid, last_seq)
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
