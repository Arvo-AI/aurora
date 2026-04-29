"""Cancel-token plumbing for agent tool calls.

A single ``ContextVar`` carries an ``asyncio.Event`` (or a thread-safe
``threading.Event`` substitute) from the outer agent runner down to deeply
nested tool wrappers and connector helpers, so they can short-circuit when the
user cancels their RCA without changing every function signature.

The orchestrator (multi-agent runner / WS handler) is responsible for:
  1. creating an ``asyncio.Event`` per RCA invocation
  2. calling ``set_cancel_token(event)`` once at the top of the run, AND
     passing the same event into worker threads via
     ``ctx.run`` / ``contextvars.copy_context()`` (LangGraph already does this
     for sync tool calls dispatched via ``loop.run_in_executor``).
  3. setting the event on cancel.

Tool wrappers and connectors call ``raise_if_cancelled()`` at safe points;
HTTP/subprocess helpers can call ``is_cancelled()`` to abort waits early.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_cancel_token_var: contextvars.ContextVar[Optional[asyncio.Event]] = contextvars.ContextVar(
    "cancel_token", default=None
)


def set_cancel_token(event: Optional[asyncio.Event]) -> contextvars.Token:
    """Bind ``event`` to the current context. Returns a token for ``reset_cancel_token``."""
    return _cancel_token_var.set(event)


def reset_cancel_token(token: contextvars.Token) -> None:
    """Reset the ContextVar to its previous value. Safe to call from any context."""
    try:
        _cancel_token_var.reset(token)
    except (ValueError, LookupError):
        # Token was created in a different Context (common when crossing
        # threadpool boundaries). Fall back to clearing the slot.
        _cancel_token_var.set(None)


def get_cancel_token() -> Optional[asyncio.Event]:
    """Return the current cancel token, or None if no run is active."""
    return _cancel_token_var.get()


def is_cancelled() -> bool:
    """Thread-safe check used inside sync tool bodies and connectors."""
    ev = _cancel_token_var.get()
    return ev is not None and ev.is_set()


def raise_if_cancelled() -> None:
    """Raise ``asyncio.CancelledError`` if the current run has been cancelled.

    Tool wrappers must NOT catch this — let it propagate so LangGraph aborts
    the node and the orchestrator finalises the run.
    """
    if is_cancelled():
        logger.info("[cancel_token] cancel signaled; raising CancelledError")
        raise asyncio.CancelledError("Cancel signaled by user")
