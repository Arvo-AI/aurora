"""Phase 6 — per-message idle-timeout watchdog.

Each in-flight assistant message gets a Redis key
``chat:idle:{session_id}:{message_id}`` whose TTL is bumped on every
``chat_events`` write (see ``chat_events.record_event``). When the key
expires, the message is considered stuck — the watchdog loop appends an
``assistant_failed`` event with ``reason=idle_timeout`` and clears
``chat_sessions.active_stream_id`` so a fresh turn can take over.

The default idle TTL is 5 minutes; tune via ``CHAT_IDLE_TIMEOUT_SECONDS``.
The scan loop runs every ``CHAT_IDLE_SCAN_INTERVAL_SECONDS`` seconds (default 30).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _idle_ttl_seconds() -> int:
    raw = os.getenv("CHAT_IDLE_TIMEOUT_SECONDS", "300")
    try:
        v = int(raw)
        return v if v > 0 else 300
    except ValueError:
        return 300


def _scan_interval_seconds() -> int:
    raw = os.getenv("CHAT_IDLE_SCAN_INTERVAL_SECONDS", "30")
    try:
        v = int(raw)
        return v if v > 0 else 30
    except ValueError:
        return 30


def idle_key(session_id: str, message_id: str) -> str:
    return f"chat:idle:{session_id}:{message_id}"


async def refresh_idle_ttl(
    session_id: str,
    message_id: str,
    ttl_seconds: Optional[int] = None,
) -> None:
    """SETEX the idle key to mark this message as still alive.

    Best-effort — Redis flap must not break the event-write path.
    """
    if not session_id or not message_id:
        return
    ttl = ttl_seconds if (ttl_seconds and ttl_seconds > 0) else _idle_ttl_seconds()
    try:
        from utils.redis.redis_stream_bus import get_async_redis
    except Exception:
        return
    client = await get_async_redis()
    if client is None:
        return
    try:
        await client.set(idle_key(session_id, message_id), "1", ex=ttl)
    except Exception as e:
        logger.warning("[idle_watchdog] refresh failed (sid=%s mid=%s): %s",
                       session_id, message_id, e)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def check_idle_expiry(session_id: str, message_id: str) -> bool:
    """Return True if the idle key is missing (expired or never set).

    Treats Redis errors as not-expired so we don't false-positive failures.
    """
    if not session_id or not message_id:
        return False
    try:
        from utils.redis.redis_stream_bus import get_async_redis
    except Exception:
        return False
    client = await get_async_redis()
    if client is None:
        return False
    try:
        exists = await client.exists(idle_key(session_id, message_id))
        return int(exists or 0) == 0
    except Exception as e:
        logger.warning("[idle_watchdog] exists failed (sid=%s mid=%s): %s",
                       session_id, message_id, e)
        return False
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def _list_active_streams_sync() -> list[tuple[str, str, str]]:
    """Return [(session_id, org_id, active_stream_id)] for sessions with a live stream.

    Uses the admin connection (no per-org RLS) — chat_sessions is org-scoped via
    org_id column but we read as a system task.
    """
    try:
        from utils.db.connection_pool import db_pool
    except Exception:
        return []

    rows: list[tuple[str, str, str]] = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, org_id, active_stream_id
                    FROM chat_sessions
                    WHERE active_stream_id IS NOT NULL
                    """
                )
                for r in cursor.fetchall() or []:
                    sid, org_id, stream_id = str(r[0]), str(r[1]), str(r[2])
                    rows.append((sid, org_id, stream_id))
    except Exception as e:
        logger.warning("[idle_watchdog] list active streams failed: %s", e)
    return rows


def _parse_message_id(active_stream_id: str, session_id: str) -> Optional[str]:
    """active_stream_id is stamped as ``{session_id}:{message_id}`` (see chat_events)."""
    if not active_stream_id:
        return None
    prefix = f"{session_id}:"
    if active_stream_id.startswith(prefix):
        return active_stream_id[len(prefix):] or None
    # Fallback: best-effort split
    parts = active_stream_id.split(":", 1)
    return parts[1] if len(parts) == 2 else None


async def _handle_idle_message(
    session_id: str,
    org_id: str,
    message_id: str,
) -> None:
    """Append assistant_failed(reason=idle_timeout) and clear active_stream_id."""
    from chat.backend.agent.utils.persistence.chat_events import (
        _clear_active_stream_id_sync,
        record_event,
    )

    logger.info("[idle_watchdog] message went idle: sid=%s mid=%s", session_id, message_id)
    try:
        await record_event(
            session_id=session_id,
            org_id=org_id,
            type="assistant_failed",
            payload={"reason": "idle_timeout"},
            message_id=message_id,
        )
    except Exception as e:
        logger.warning("[idle_watchdog] record_event failed: %s", e)

    # record_event clears active_stream_id on terminal events, but belt-and-suspenders:
    try:
        await asyncio.to_thread(
            _clear_active_stream_id_sync,
            session_id=session_id,
            org_id=org_id,
            expected_stream_id=f"{session_id}:{message_id}",
        )
    except Exception as e:
        logger.warning("[idle_watchdog] clear active_stream_id failed: %s", e)


async def _scan_once() -> int:
    """Run a single scan pass. Returns count of messages transitioned to failed.

    Uses one Redis client + a pipelined EXISTS for all active streams to avoid
    a connect/close cycle per session.
    """
    streams = await asyncio.to_thread(_list_active_streams_sync)
    if not streams:
        return 0

    candidates: list[tuple[str, str, str]] = []  # (session_id, org_id, message_id)
    for session_id, org_id, active_stream_id in streams:
        message_id = _parse_message_id(active_stream_id, session_id)
        if message_id:
            candidates.append((session_id, org_id, message_id))
    if not candidates:
        return 0

    try:
        from utils.redis.redis_stream_bus import get_async_redis
    except Exception:
        return 0
    client = await get_async_redis()
    if client is None:
        return 0
    try:
        pipe = client.pipeline(transaction=False)
        for session_id, _org_id, message_id in candidates:
            pipe.exists(idle_key(session_id, message_id))
        results = await pipe.execute()
    except Exception as e:
        logger.warning("[idle_watchdog] pipelined exists failed: %s", e)
        return 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

    failed = 0
    for (session_id, org_id, message_id), exists in zip(candidates, results):
        if int(exists or 0) == 0:
            await _handle_idle_message(session_id, org_id, message_id)
            failed += 1
    return failed


async def idle_watchdog_loop() -> None:
    """Forever-loop scanning every CHAT_IDLE_SCAN_INTERVAL_SECONDS.

    Resilient to per-iteration errors — never exits unless cancelled.
    """
    interval = _scan_interval_seconds()
    logger.info(
        "[idle_watchdog] starting loop (idle_ttl=%ss scan_interval=%ss)",
        _idle_ttl_seconds(), interval,
    )
    while True:
        try:
            await _scan_once()
        except asyncio.CancelledError:
            logger.info("[idle_watchdog] cancelled, exiting")
            raise
        except Exception as e:
            logger.warning("[idle_watchdog] scan iteration failed: %s", e)
        await asyncio.sleep(interval)
