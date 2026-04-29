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
    "subagent_dispatched",
    "subagent_finished",
    "subagent_failed",
    "plan_committed",
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
    from utils.db.connection_pool import db_pool

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

    return seq


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
