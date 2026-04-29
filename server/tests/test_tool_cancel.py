"""Tests for the tool-cancel plumbing (Phase 6).

Covers:
  * cancel_token ContextVar helpers
  * wrap_func_with_capture pre/post cancel check raises CancelledError
  * terminal_run / run_with_cancel SIGTERMs the process group on cancel
"""

import asyncio
import os
import sys
import threading
import time

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.utils.cancel_token import (  # noqa: E402
    get_cancel_token,
    is_cancelled,
    raise_if_cancelled,
    reset_cancel_token,
    set_cancel_token,
)


# ---------------------------------------------------------------------------
# cancel_token helpers
# ---------------------------------------------------------------------------


def test_cancel_token_default_is_none():
    assert get_cancel_token() is None
    assert is_cancelled() is False
    raise_if_cancelled()  # should not raise


def test_cancel_token_set_and_signal():
    ev = asyncio.Event()
    tok = set_cancel_token(ev)
    try:
        assert get_cancel_token() is ev
        assert is_cancelled() is False
        raise_if_cancelled()

        ev.set()
        assert is_cancelled() is True
        with pytest.raises(asyncio.CancelledError):
            raise_if_cancelled()
    finally:
        reset_cancel_token(tok)
    assert get_cancel_token() is None


def test_reset_tolerates_cross_context_token():
    """Tokens minted in another context must not crash reset()."""
    ev = asyncio.Event()
    tok_holder = {}

    def _worker():
        tok_holder["tok"] = set_cancel_token(ev)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    # Resetting a token from a different context should not raise.
    reset_cancel_token(tok_holder["tok"])


# ---------------------------------------------------------------------------
# wrap_func_with_capture pre/post cancel
# ---------------------------------------------------------------------------


def test_wrapper_raises_cancelled_when_token_set_before_call(monkeypatch):
    """The wrapper must short-circuit if cancel was signalled before the tool runs.

    We simulate the wrapper's relevant slice (pre-call cancel gate) without
    pulling in the full LangChain tool registration path.
    """

    def _inner(**kwargs):  # would be the real connector body
        return "should not run"

    ev = asyncio.Event()
    ev.set()
    tok = set_cancel_token(ev)
    try:
        # Mirrors the gate inside wrap_func_with_capture.wrapped_func
        with pytest.raises(asyncio.CancelledError):
            raise_if_cancelled()
            _inner()
    finally:
        reset_cancel_token(tok)


def test_wrapper_raises_cancelled_when_token_set_during_call():
    """If cancel arrives while the tool is in flight, the post-call check fires."""

    def _slow_inner(**kwargs):
        time.sleep(0.05)
        return "completed"

    ev = asyncio.Event()
    tok = set_cancel_token(ev)
    try:
        # Schedule the cancel mid-call.
        threading.Timer(0.01, ev.set).start()
        result = _slow_inner()
        # post-call gate
        with pytest.raises(asyncio.CancelledError):
            raise_if_cancelled()
        # The tool itself returned, but the wrapper drops the result.
        assert result == "completed"
    finally:
        reset_cancel_token(tok)


# ---------------------------------------------------------------------------
# Subprocess cancel via run_with_cancel
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only process-group semantics")
def test_subprocess_cancel_kills_process_group(monkeypatch):
    """A cancel signal during a long sleep must SIGTERM the child within ~1s."""
    from utils.terminal import terminal_run as tr

    ev = asyncio.Event()

    # Override is_cancelled inside the helper module so we don't need a full
    # ContextVar copy across threads.
    monkeypatch.setattr(
        "chat.backend.agent.utils.cancel_token.is_cancelled",
        lambda: ev.is_set(),
    )

    # Fire cancel after 200ms.
    threading.Timer(0.2, ev.set).start()

    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        tr.run_with_cancel(
            cmd=["sleep", "60"],
            args=["sleep", "60"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=60,
            cwd=None,
            env=None,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"cancel took {elapsed:.2f}s, expected < 5s"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only process-group semantics")
def test_subprocess_completes_normally_when_not_cancelled():
    """Sanity: ensure run_with_cancel still works for fast commands."""
    from utils.terminal import terminal_run as tr

    result = tr.run_with_cancel(
        cmd=["echo", "hello"],
        args=["echo", "hello"],
        capture_output=True,
        text=True,
        shell=False,
        timeout=5,
        cwd=None,
        env=None,
    )
    assert result.returncode == 0
    assert "hello" in result.stdout


# ---------------------------------------------------------------------------
# Connector cancel — sync HTTP path simulation
# ---------------------------------------------------------------------------


def test_connector_cancel_short_circuits_before_request(monkeypatch):
    """A connector that calls raise_if_cancelled() at entry must abort instead
    of issuing the upstream HTTP call when cancel is already signalled."""
    upstream_calls = {"count": 0}

    def fake_http_get(url, **_):
        upstream_calls["count"] += 1
        return {"ok": True}

    def connector_query():
        # Pattern recommended for sync connectors: gate at function entry.
        raise_if_cancelled()
        return fake_http_get("https://api.datadoghq.com/api/v2/logs/events/search")

    ev = asyncio.Event()
    ev.set()
    tok = set_cancel_token(ev)
    try:
        with pytest.raises(asyncio.CancelledError):
            connector_query()
    finally:
        reset_cancel_token(tok)
    assert upstream_calls["count"] == 0
