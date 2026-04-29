"""Append-only chat_events log + chat_messages projection writers (Phase 2 dual-write)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional


import psycopg2

logger = logging.getLogger(__name__)

WsSender = Callable[[dict], Awaitable[None]]

_active_ws_sender_var: contextvars.ContextVar[Optional[WsSender]] = contextvars.ContextVar(
    "active_ws_sender", default=None
)

_WS_BROADCAST_EVENT_TYPES: frozenset[str] = frozenset({
    "user_message",
    "assistant_started",
    "assistant_chunk",
    "tool_call_started",
    "tool_call_chunk",
    "tool_call_result",
    "plan_committed",
    "subagent_dispatched",
    "subagent_finished",
    "subagent_failed",
    "assistant_finalized",
    "assistant_interrupted",
    "assistant_failed",
})


def set_active_ws_sender(sender: Optional[WsSender]) -> contextvars.Token:
    return _active_ws_sender_var.set(sender)


def reset_active_ws_sender(token: contextvars.Token) -> None:
    try:
        _active_ws_sender_var.reset(token)
    except Exception:
        pass

EVENT_TYPES: frozenset[str] = frozenset({
    "user_message",
    "assistant_started",
    "assistant_chunk",
    "tool_call_started",
    "tool_call_chunk",
    "tool_call_result",
    "plan_committed",
    "subagent_dispatched",
    "subagent_finished",
    "subagent_failed",
    "assistant_finalized",
    "assistant_interrupted",
    "assistant_failed",
})

_MAX_SEQ_RETRIES = 5
_LOG_PREFIX_FAIL = "[chat_events:dual_write_failed]"

_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({
    "assistant_finalized",
    "assistant_interrupted",
    "assistant_failed",
})


def _set_rls(cursor, conn, org_id: str) -> None:
    cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
    conn.commit()


def _insert_event_sync(
    session_id: str,
    org_id: str,
    type_: str,
    payload: dict,
    message_id: Optional[str],
    agent_id: Optional[str],
    parent_agent_id: Optional[str],
    payload_schema_version: int,
) -> int:
    """Append one chat_events row.

    Returns:
      seq (>=1) on success.
      0 when a terminal-event UNIQUE conflict is hit (another writer won; no-op).
      Raises RuntimeError if the seq-race retry budget is exhausted.
    """
    from utils.db.connection_pool import db_pool

    is_terminal = type_ in _TERMINAL_EVENT_TYPES and message_id is not None
    last_err: Optional[Exception] = None
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            for _ in range(_MAX_SEQ_RETRIES):
                try:
                    cursor.execute(
                        """
                        INSERT INTO chat_events
                            (session_id, seq, org_id, agent_id, parent_agent_id,
                             type, payload, payload_schema_version, message_id)
                        SELECT %s,
                               COALESCE((SELECT MAX(seq) FROM chat_events WHERE session_id = %s), 0) + 1,
                               %s, %s, %s, %s, %s, %s, %s
                        RETURNING seq
                        """,
                        (
                            session_id,
                            session_id,
                            org_id,
                            agent_id,
                            parent_agent_id,
                            type_,
                            json.dumps(payload),
                            payload_schema_version,
                            message_id,
                        ),
                    )
                    row = cursor.fetchone()
                    conn.commit()
                    return int(row[0]) if row else -1
                except psycopg2.IntegrityError as e:
                    conn.rollback()
                    last_err = e
                    # Terminal-event UNIQUE conflict — another writer already finalized
                    # this message. Treat as no-op.
                    if is_terminal and "uq_chat_events_terminal_per_msg" in str(e):
                        logger.info(
                            "[chat_events] terminal-event idempotency: %s already recorded for msg=%s",
                            type_, message_id,
                        )
                        return 0
                    continue
    raise RuntimeError(f"chat_events seq race exceeded retries: {last_err}")


async def record_event(
    session_id: str,
    org_id: str,
    type: str,
    payload: dict,
    *,
    message_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
    payload_schema_version: int = 1,
) -> int:
    if type not in EVENT_TYPES:
        logger.warning("%s unknown event type: %s", _LOG_PREFIX_FAIL, type)
        return -1
    if not session_id or not org_id:
        logger.warning("%s missing session_id/org_id (type=%s)", _LOG_PREFIX_FAIL, type)
        return -1
    try:
        seq = await asyncio.to_thread(
            _insert_event_sync,
            session_id,
            org_id,
            type,
            payload,
            message_id,
            agent_id,
            parent_agent_id,
            payload_schema_version,
        )
    except Exception as e:
        logger.warning("%s record_event(type=%s): %s", _LOG_PREFIX_FAIL, type, e)
        return -1

    if type in _WS_BROADCAST_EVENT_TYPES:
        sender = _active_ws_sender_var.get()
        if sender is not None:
            try:
                await sender({
                    "type": type,
                    "data": {
                        **payload,
                        "agent_id": agent_id,
                        "parent_agent_id": parent_agent_id,
                    },
                    "session_id": session_id,
                })
            except Exception as e:
                logger.warning("ws-broadcast failed (type=%s): %s", type, e)

    # Publish to Redis Stream + wake channel for SSE listeners.
    # seq==0 means an idempotent terminal collision — already published by the
    # original writer; skip the redis fan-out so we don't double-publish.
    if seq > 0 and message_id:
        try:
            from utils.redis.redis_stream_bus import xadd_event, publish_wake
            await xadd_event(
                session_id=session_id,
                message_id=message_id,
                type_=type,
                payload=payload,
                seq=seq,
                agent_id=agent_id,
                parent_agent_id=parent_agent_id,
            )
            await publish_wake(session_id)
        except Exception as e:
            logger.warning("[chat_events] redis publish failed (type=%s): %s", type, e)

        # Skip idle-watchdog TTL refresh for terminal events — the message is
        # done so no more progress is expected; the watchdog scan just races
        # against the active_stream_id clear below.
        if type not in _TERMINAL_EVENT_TYPES:
            try:
                from chat.backend.agent.utils.idle_watchdog import refresh_idle_ttl
                await refresh_idle_ttl(session_id, message_id)
            except Exception as e:
                logger.warning("[chat_events] idle TTL refresh failed: %s", e)

    # Maintain chat_sessions.active_stream_id for the Vercel resumable-stream
    # race fix: clear-then-set on assistant_started; clear on terminals.
    if message_id and type == "assistant_started":
        try:
            await asyncio.to_thread(
                _set_active_stream_id_sync,
                session_id=session_id,
                org_id=org_id,
                stream_id=f"{session_id}:{message_id}",
            )
        except Exception as e:
            logger.warning("[chat_events] set active_stream_id failed: %s", e)
    elif message_id and type in _TERMINAL_EVENT_TYPES:
        try:
            await asyncio.to_thread(
                _clear_active_stream_id_sync,
                session_id=session_id,
                org_id=org_id,
                expected_stream_id=f"{session_id}:{message_id}",
            )
        except Exception as e:
            logger.warning("[chat_events] clear active_stream_id failed: %s", e)

    return seq


def _set_active_stream_id_sync(*, session_id: str, org_id: str, stream_id: str) -> None:
    """Clear-then-set active_stream_id (Vercel resumable-stream race fix)."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                "UPDATE chat_sessions SET active_stream_id = %s WHERE id = %s",
                (stream_id, session_id),
            )
            conn.commit()


def _clear_active_stream_id_sync(
    *, session_id: str, org_id: str, expected_stream_id: str
) -> None:
    """Clear active_stream_id only if it still matches our message — guards against
    a fresh turn having taken over the slot."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                """
                UPDATE chat_sessions SET active_stream_id = NULL
                WHERE id = %s AND active_stream_id = %s
                """,
                (session_id, expected_stream_id),
            )
            conn.commit()


async def get_active_stream_id(*, session_id: str, org_id: str) -> Optional[str]:
    """Read chat_sessions.active_stream_id. Returns None if NULL or on error."""
    if not session_id or not org_id:
        return None

    def _read() -> Optional[str]:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, org_id)
                cursor.execute(
                    "SELECT active_stream_id FROM chat_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
                return row[0] if row and row[0] else None

    try:
        return await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning("[chat_events] get_active_stream_id failed: %s", e)
        return None


async def fetch_events_after(
    *, session_id: str, org_id: str, after_seq: int, limit: int = 5000
) -> list[dict]:
    """Replay chat_events with seq > after_seq, ordered by seq."""
    if not session_id or not org_id:
        return []

    def _read() -> list[dict]:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, org_id)
                cursor.execute(
                    """
                    SELECT seq, type, payload, agent_id, parent_agent_id, message_id
                    FROM chat_events
                    WHERE session_id = %s AND seq > %s
                    ORDER BY seq ASC
                    LIMIT %s
                    """,
                    (session_id, after_seq, limit),
                )
                rows = cursor.fetchall() or []
                return [
                    {
                        "seq": int(r[0]),
                        "type": r[1],
                        "payload": r[2] if isinstance(r[2], dict) else (json.loads(r[2]) if r[2] else {}),
                        "agent_id": r[3],
                        "parent_agent_id": r[4],
                        "message_id": str(r[5]) if r[5] else None,
                    }
                    for r in rows
                ]

    try:
        return await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning("[chat_events] fetch_events_after failed: %s", e)
        return []


def _upsert_projection_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    status: str,
    parts: list[dict],
    agent_id: Optional[str],
    metadata: Optional[dict],
    seq: Optional[int],
    finalized: bool,
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)

            resolved_seq = seq
            if resolved_seq is None:
                cursor.execute(
                    "SELECT MAX(seq) FROM chat_events WHERE session_id = %s AND message_id = %s",
                    (session_id, message_id),
                )
                row = cursor.fetchone()
                if row and row[0] is not None:
                    resolved_seq = int(row[0])
                else:
                    resolved_seq = _next_seq(cursor, session_id)

            now = datetime.now(timezone.utc)
            finalized_at = now if finalized else None
            cursor.execute(
                """
                INSERT INTO chat_messages
                    (id, session_id, seq, role, agent_id, status, parts,
                     metadata, org_id, created_at, updated_at, finalized_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    seq = EXCLUDED.seq,
                    role = EXCLUDED.role,
                    agent_id = EXCLUDED.agent_id,
                    status = EXCLUDED.status,
                    parts = EXCLUDED.parts,
                    metadata = COALESCE(EXCLUDED.metadata, chat_messages.metadata),
                    updated_at = EXCLUDED.updated_at,
                    finalized_at = COALESCE(EXCLUDED.finalized_at, chat_messages.finalized_at)
                """,
                (
                    message_id,
                    session_id,
                    resolved_seq,
                    role,
                    agent_id,
                    status,
                    json.dumps(parts),
                    json.dumps(metadata) if metadata is not None else None,
                    org_id,
                    now,
                    now,
                    finalized_at,
                ),
            )
            conn.commit()


async def upsert_message_projection(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    status: str,
    parts: list[dict],
    agent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    seq: Optional[int] = None,
    finalized: bool = False,
) -> None:
    if not message_id or not session_id or not org_id:
        logger.warning(
            "%s upsert_message_projection missing required ids (msg=%s session=%s org=%s)",
            _LOG_PREFIX_FAIL, bool(message_id), bool(session_id), bool(org_id),
        )
        return
    try:
        await asyncio.to_thread(
            _upsert_projection_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            role=role,
            status=status,
            parts=parts,
            agent_id=agent_id,
            metadata=metadata,
            seq=seq,
            finalized=finalized,
        )
    except Exception as e:
        logger.warning("%s upsert_message_projection(role=%s): %s", _LOG_PREFIX_FAIL, role, e)


def ensure_message_id(message: dict) -> str:
    mid = message.get("id")
    if not mid:
        mid = str(uuid.uuid4())
        message["id"] = mid
    return mid


# -----------------------------------------------------------------------------
# Streaming projection helpers — incrementally mutate chat_messages.parts[]
# as events arrive, instead of rewriting the whole list each time.
# Parts shape: AI SDK 5 UIMessage discriminated union
#   {type: "text", text, state}
#   {type: "reasoning", text, state}
#   {type: "tool-<name>", toolCallId, state, input?, output?, errorText?}
#   {type: "data-<name>", id?, data}
# -----------------------------------------------------------------------------


def _append_part_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    agent_id: Optional[str],
    part: dict,
    status: str,
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            now = datetime.now(timezone.utc)
            cursor.execute(
                """
                INSERT INTO chat_messages
                    (id, session_id, seq, role, agent_id, status, parts,
                     org_id, created_at, updated_at)
                VALUES (
                    %s, %s,
                    COALESCE((SELECT MAX(seq) FROM chat_messages WHERE session_id = %s), 0) + 1,
                    %s, %s, %s, %s::jsonb, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    parts = chat_messages.parts || EXCLUDED.parts,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    message_id,
                    session_id,
                    session_id,
                    role,
                    agent_id,
                    status,
                    json.dumps([part]),
                    org_id,
                    now,
                    now,
                ),
            )
            conn.commit()


async def append_message_part(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    part: dict,
    agent_id: Optional[str] = None,
    status: str = "streaming",
) -> None:
    if not message_id or not session_id or not org_id:
        return
    try:
        await asyncio.to_thread(
            _append_part_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            role=role,
            agent_id=agent_id,
            part=part,
            status=status,
        )
    except Exception as e:
        logger.warning("%s append_message_part: %s", _LOG_PREFIX_FAIL, e)


def _extend_text_part_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    agent_id: Optional[str],
    delta: str,
    part_type: str,
) -> None:
    """Append `delta` to the last `part_type` (text|reasoning) part, or start a new one.
    Avoids one part per token."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                "SELECT parts FROM chat_messages WHERE id = %s FOR UPDATE",
                (message_id,),
            )
            row = cursor.fetchone()
            now = datetime.now(timezone.utc)
            if row is None:
                parts = [{"type": part_type, "text": delta, "state": "streaming"}]
                cursor.execute(
                    """
                    INSERT INTO chat_messages
                        (id, session_id, seq, role, agent_id, status, parts,
                         org_id, created_at, updated_at)
                    VALUES (
                        %s, %s,
                        COALESCE((SELECT MAX(seq) FROM chat_messages WHERE session_id = %s), 0) + 1,
                        %s, %s, 'streaming', %s::jsonb, %s, %s, %s
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        message_id, session_id, session_id, role, agent_id,
                        json.dumps(parts), org_id, now, now,
                    ),
                )
                conn.commit()
                return
            parts = list(row[0] or [])
            if parts and isinstance(parts[-1], dict) and parts[-1].get("type") == part_type \
                    and parts[-1].get("state") == "streaming":
                parts[-1]["text"] = (parts[-1].get("text") or "") + delta
            else:
                parts.append({"type": part_type, "text": delta, "state": "streaming"})
            cursor.execute(
                "UPDATE chat_messages SET parts = %s::jsonb, updated_at = %s WHERE id = %s",
                (json.dumps(parts), now, message_id),
            )
            conn.commit()


async def extend_text_part(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    role: str,
    delta: str,
    agent_id: Optional[str] = None,
    part_type: str = "text",
) -> None:
    if not message_id or not session_id or not org_id or not delta:
        return
    try:
        await asyncio.to_thread(
            _extend_text_part_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            role=role,
            agent_id=agent_id,
            delta=delta,
            part_type=part_type,
        )
    except Exception as e:
        logger.warning("%s extend_text_part: %s", _LOG_PREFIX_FAIL, e)


def _update_tool_part_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    tool_call_id: str,
    state: str,
    output: Any,
    error_text: Optional[str],
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                "SELECT parts FROM chat_messages WHERE id = %s AND session_id = %s FOR UPDATE",
                (message_id, session_id),
            )
            row = cursor.fetchone()
            if row is None:
                return
            parts = list(row[0] or [])
            for p in parts:
                if isinstance(p, dict) \
                        and isinstance(p.get("type"), str) \
                        and p["type"].startswith("tool-") \
                        and p.get("toolCallId") == tool_call_id:
                    p["state"] = state
                    if output is not None:
                        p["output"] = output
                    if error_text is not None:
                        p["errorText"] = error_text
                    break
            cursor.execute(
                "UPDATE chat_messages SET parts = %s::jsonb, updated_at = NOW() "
                "WHERE id = %s AND session_id = %s",
                (json.dumps(parts), message_id, session_id),
            )
            conn.commit()


async def update_tool_part(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    tool_call_id: str,
    state: str,
    output: Any = None,
    error_text: Optional[str] = None,
) -> None:
    if not message_id or not session_id or not org_id or not tool_call_id:
        return
    try:
        await asyncio.to_thread(
            _update_tool_part_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            tool_call_id=tool_call_id,
            state=state,
            output=output,
            error_text=error_text,
        )
    except Exception as e:
        logger.warning("%s update_tool_part: %s", _LOG_PREFIX_FAIL, e)


def _update_data_part_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    data_type: str,
    match_key: str,
    match_value: str,
    patch: dict,
) -> None:
    """Find a data-<name> part where part['data'][match_key] == match_value, merge `patch` into it.
    If no such part exists, append a new one with `data = patch`."""
    from utils.db.connection_pool import db_pool

    full_type = f"data-{data_type}"
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                "SELECT parts FROM chat_messages WHERE id = %s AND session_id = %s FOR UPDATE",
                (message_id, session_id),
            )
            row = cursor.fetchone()
            if row is None:
                return
            parts = list(row[0] or [])
            updated = False
            for p in parts:
                if isinstance(p, dict) and p.get("type") == full_type \
                        and isinstance(p.get("data"), dict) \
                        and p["data"].get(match_key) == match_value:
                    p["data"].update(patch)
                    updated = True
                    break
            if not updated:
                parts.append({"type": full_type, "data": dict(patch)})
            cursor.execute(
                "UPDATE chat_messages SET parts = %s::jsonb, updated_at = NOW() "
                "WHERE id = %s AND session_id = %s",
                (json.dumps(parts), message_id, session_id),
            )
            conn.commit()


async def upsert_data_part(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    data_type: str,
    match_key: str,
    match_value: str,
    patch: dict,
) -> None:
    if not message_id or not session_id or not org_id or not data_type \
            or not match_key or not match_value:
        return
    try:
        await asyncio.to_thread(
            _update_data_part_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            data_type=data_type,
            match_key=match_key,
            match_value=match_value,
            patch=patch,
        )
    except Exception as e:
        logger.warning("%s upsert_data_part: %s", _LOG_PREFIX_FAIL, e)


def _finalize_message_sync(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    status: str,
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            _set_rls(cursor, conn, org_id)
            cursor.execute(
                "SELECT parts FROM chat_messages WHERE id = %s AND session_id = %s FOR UPDATE",
                (message_id, session_id),
            )
            row = cursor.fetchone()
            now = datetime.now(timezone.utc)
            if row is None:
                # No row to finalize — treat as no-op (caller may not have written any parts).
                return
            parts = list(row[0] or [])
            for p in parts:
                if isinstance(p, dict) and p.get("state") == "streaming":
                    p["state"] = "done"
            cursor.execute(
                """
                UPDATE chat_messages SET
                    parts = %s::jsonb,
                    status = %s,
                    finalized_at = %s,
                    updated_at = %s
                WHERE id = %s AND session_id = %s
                """,
                (json.dumps(parts), status, now, now, message_id, session_id),
            )
            conn.commit()


async def finalize_message(
    *,
    message_id: str,
    session_id: str,
    org_id: str,
    status: str,
) -> None:
    if status not in ("complete", "interrupted", "failed"):
        logger.warning("%s finalize_message bad status: %s", _LOG_PREFIX_FAIL, status)
        return
    if not message_id or not session_id or not org_id:
        return
    try:
        await asyncio.to_thread(
            _finalize_message_sync,
            message_id=message_id,
            session_id=session_id,
            org_id=org_id,
            status=status,
        )
    except Exception as e:
        logger.warning("%s finalize_message: %s", _LOG_PREFIX_FAIL, e)
