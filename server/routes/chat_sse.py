"""Chat SSE transport.

Endpoints
---------
GET  /api/chat/stream            — SSE stream of chat_events for a session
POST /api/chat/messages          — submit a user message; spawn workflow; return stream URL
POST /api/chat/cancel            — request graceful interrupt of the active turn
POST /api/chat/confirmations     — relay an approve/decline answer to a paused tool call
POST /api/chat/direct-tool       — invoke a tool directly without LLM round-trip

Frame format (SSE)
------------------
    event: <type>
    data:  <json>
    id:    <chat_events.seq>
    \n

Heartbeats are bare ``:heartbeat\\n\\n`` lines emitted every 30s.

Resume
------
Clients may set ``Last-Event-ID: <seq>``. We replay chat_events with seq > last,
emit ``event: meta:resumed`` then tail the active Redis Stream until a terminal
event is observed (``event: meta:completed``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request, stream_with_context

from chat.backend.agent.utils.persistence.chat_events import (
    EVENT_TYPES,
    fetch_events_after,
    get_active_stream_id,
    record_event,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.rate_limit.chat_rate import is_allowed as chat_rate_is_allowed
from utils.redis.redis_stream_bus import (
    cancel_channel,
    get_async_redis,
    read_stream_replay,
    tail_stream,
    wake_channel,
)

logger = logging.getLogger(__name__)

chat_sse_bp = Blueprint("chat_sse", __name__)

_SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
}

_TERMINAL_EVENT_TYPES = frozenset({
    "assistant_finalized",
    "assistant_interrupted",
    "assistant_failed",
})

_HEARTBEAT_INTERVAL_S = 30.0
_REPLAY_LIMIT = 5_000


# ---------------------------------------------------------------------------
# SSE wire helpers
# ---------------------------------------------------------------------------


def _format_frame(event_type: str, data: dict, seq: Optional[int]) -> bytes:
    """Serialize one SSE frame with ``id:`` set to chat_events.seq."""
    payload = json.dumps(data, default=str)
    parts = [f"event: {event_type}", f"data: {payload}"]
    if seq is not None:
        parts.append(f"id: {seq}")
    return ("\n".join(parts) + "\n\n").encode("utf-8")


def _wire_data(*, seq: int, session_id: str, type_: str, payload: dict,
               message_id: Optional[str], agent_id: Optional[str],
               parent_agent_id: Optional[str]) -> dict:
    return {
        "seq": seq,
        "session_id": session_id,
        "message_id": message_id,
        "agent_id": agent_id,
        "parent_agent_id": parent_agent_id,
        "type": type_,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# GET /api/chat/stream
# ---------------------------------------------------------------------------


@chat_sse_bp.route("/api/chat/stream", methods=["GET"])
@require_permission("chat", "read")
def chat_stream(user_id: str):
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "no org context"}), 403

    last_event_id_raw = request.headers.get("Last-Event-ID") or "0"
    try:
        last_event_id = int(last_event_id_raw)
    except ValueError:
        last_event_id = 0

    # Pre-flight: if no active stream and no replay backlog, return 204.
    pre_check = _has_anything_to_stream(session_id, org_id, last_event_id)
    if pre_check is None:
        # Redis unreachable AND postgres lookup failed — degrade.
        return jsonify({"error": "stream backend unavailable", "retry_after": 2}), 503
    has_active, replay_backlog = pre_check
    if not has_active and not replay_backlog:
        return Response(status=204)

    @stream_with_context
    def generate():
        loop = asyncio.new_event_loop()
        try:
            yield from _drive_sse(
                loop=loop,
                session_id=session_id,
                org_id=org_id,
                last_event_id=last_event_id,
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass

    return Response(generate(), headers=_SSE_HEADERS)


def _has_anything_to_stream(
    session_id: str, org_id: str, last_event_id: int
) -> Optional[tuple[bool, bool]]:
    """Return (has_active_stream, has_replay_backlog) or None on backend failure."""
    try:
        loop = asyncio.new_event_loop()
        try:
            active = loop.run_until_complete(
                get_active_stream_id(session_id=session_id, org_id=org_id)
            )
            backlog = loop.run_until_complete(
                fetch_events_after(
                    session_id=session_id,
                    org_id=org_id,
                    after_seq=last_event_id,
                    limit=1,
                )
            )
        finally:
            loop.close()
        return (bool(active), bool(backlog))
    except Exception as e:
        logger.warning("[chat_sse] preflight failed: %s", e)
        return None


def _drive_sse(*, loop: asyncio.AbstractEventLoop, session_id: str,
               org_id: str, last_event_id: int):
    """Synchronous generator that yields SSE bytes. Drives an asyncio loop
    underneath so we can use redis.asyncio while satisfying Flask's sync IO.
    """
    asyncio.set_event_loop(loop)
    last_seq = last_event_id
    seen_terminal = False
    # Track the latest non-terminal message_id from backlog so we can tail its
    # Redis Stream even if `active_stream_id` happens to be NULL when we look
    # (race: SSE GET can arrive between user_message and assistant_started writes).
    backlog_message_id: Optional[str] = None

    # --- 1. Replay chat_events (durable backlog) -----------------------------
    try:
        backlog = loop.run_until_complete(
            fetch_events_after(
                session_id=session_id,
                org_id=org_id,
                after_seq=last_seq,
                limit=_REPLAY_LIMIT,
            )
        )
    except Exception as e:
        logger.warning("[chat_sse] replay query failed: %s", e)
        backlog = []
    for ev in backlog:
        if ev["type"] not in EVENT_TYPES:
            continue
        yield _format_frame(
            ev["type"],
            _wire_data(
                seq=ev["seq"],
                session_id=session_id,
                type_=ev["type"],
                payload=ev.get("payload") or {},
                message_id=ev.get("message_id"),
                agent_id=ev.get("agent_id"),
                parent_agent_id=ev.get("parent_agent_id"),
            ),
            seq=ev["seq"],
        )
        last_seq = max(last_seq, ev["seq"])
        if ev["type"] in _TERMINAL_EVENT_TYPES:
            seen_terminal = True
        elif ev.get("message_id"):
            backlog_message_id = ev["message_id"]

    # --- 2. meta:resumed marker ---------------------------------------------
    yield _format_frame("meta:resumed", {"resumed_from": last_event_id}, seq=None)

    if seen_terminal:
        yield _format_frame("meta:completed", {"last_seq": last_seq}, seq=None)
        return

    # --- 3. Tail the active Redis Stream ------------------------------------
    active = loop.run_until_complete(
        get_active_stream_id(session_id=session_id, org_id=org_id)
    )
    message_id: Optional[str] = None
    if active:
        try:
            _, message_id = active.split(":", 1)
        except ValueError:
            logger.warning("[chat_sse] malformed active_stream_id=%s", active)
            message_id = None

    # Fallback: backlog showed a non-terminal turn started, but active_stream_id
    # was unset at lookup time. Tail that message_id anyway — the Celery worker
    # is writing to chat:stream:{session}:{message_id} regardless.
    if not message_id and backlog_message_id:
        logger.info(
            "[chat_sse] active_stream_id missing, tailing backlog message_id=%s",
            backlog_message_id,
        )
        message_id = backlog_message_id

    if not message_id:
        yield _format_frame("meta:completed", {"last_seq": last_seq}, seq=None)
        return

    # Drain anything that landed between the DB replay and now, then tail live.
    try:
        replay_entries = loop.run_until_complete(
            read_stream_replay(
                session_id=session_id, message_id=message_id, from_id="0"
            )
        )
    except Exception as e:
        logger.warning("[chat_sse] redis replay failed: %s", e)
        replay_entries = []
    # Track the highest entry_id we've seen so XREAD can resume from there
    # rather than `$`. Using `$` here would lose any event XADD'd between
    # replay returning and XREAD being registered — including a possibly
    # terminal assistant_finalized — leaving the SSE stuck on heartbeats.
    replay_max_entry_id: Optional[str] = None
    for entry in replay_entries:
        eid = entry.get("entry_id")
        if eid:
            replay_max_entry_id = eid
        if entry["seq"] <= last_seq or entry["type"] not in EVENT_TYPES:
            continue
        yield _format_frame(
            entry["type"],
            _wire_data(
                seq=entry["seq"],
                session_id=session_id,
                type_=entry["type"],
                payload=entry["payload"],
                message_id=entry.get("message_id"),
                agent_id=entry.get("agent_id"),
                parent_agent_id=entry.get("parent_agent_id"),
            ),
            seq=entry["seq"],
        )
        last_seq = max(last_seq, entry["seq"])
        if entry["type"] in _TERMINAL_EVENT_TYPES:
            yield _format_frame("meta:completed", {"last_seq": last_seq}, seq=None)
            return

    # Per-entry tail loop: each call to xread blocks up to TAIL_BLOCK_MS, returns
    # the entries that landed, we yield each one as an SSE frame, then re-arm.
    # The Redis client is built ONCE here and reused — building per-iteration
    # cost a connect/ping/close every TAIL_BLOCK_MS for every open SSE stream.
    TAIL_BLOCK_MS = 2_000
    # Resume from the last replay entry_id (XREAD returns events strictly after
    # the cursor) so we don't drop anything XADD'd during the replay→tail gap.
    # If the replay was empty, "0-0" makes XREAD return everything in the stream
    # on the first call — _xread_once below filters by seq <= last_seq anyway.
    cursor = replay_max_entry_id or "0-0"
    last_heartbeat = time.monotonic()
    from utils.redis.redis_stream_bus import get_async_redis, stream_key
    stream_redis_key = stream_key(session_id, message_id)
    tail_client = loop.run_until_complete(get_async_redis())
    if tail_client is None:
        yield _format_frame("meta:completed", {"last_seq": last_seq}, seq=None)
        return

    async def _xread_once(cur: str) -> list[tuple[str, dict]]:
        """One xread call against the long-lived `tail_client`."""
        try:
            resp = await tail_client.xread({stream_redis_key: cur}, count=100, block=TAIL_BLOCK_MS)
        except Exception as e:
            logger.warning("[chat_sse] xread failed: %s", e)
            return []
        if not resp:
            return []
        out: list[tuple[str, dict]] = []
        for _stream_key, entries in resp:
            for entry_id, fields in entries:
                payload_raw = fields.get("payload") or "{}"
                try:
                    payload = json.loads(payload_raw)
                except Exception:
                    payload = {}
                out.append((entry_id, {
                    "seq": int(fields.get("seq") or 0),
                    "type": fields.get("type") or "",
                    "payload": payload,
                    "agent_id": fields.get("agent_id") or None,
                    "parent_agent_id": fields.get("parent_agent_id") or None,
                    "message_id": fields.get("message_id") or None,
                }))
        return out

    try:
        while True:
            try:
                batch = loop.run_until_complete(_xread_once(cursor))
            except GeneratorExit:
                return
            except Exception as e:
                logger.warning("[chat_sse] tail loop error: %s", e)
                return

            if not batch:
                now = time.monotonic()
                if (now - last_heartbeat) >= _HEARTBEAT_INTERVAL_S:
                    yield b":heartbeat\n\n"
                    last_heartbeat = now
                continue

            for entry_id, entry in batch:
                cursor = entry_id
                if entry["type"] not in EVENT_TYPES:
                    continue
                # Belt-and-suspenders: when cursor seeded from "0-0" the first
                # XREAD can replay entries already delivered via the DB or
                # Redis backlog above. Skip anything <= last_seq so the client
                # never sees duplicate parts.
                if entry["seq"] <= last_seq:
                    continue
                yield _format_frame(
                    entry["type"],
                    _wire_data(
                        seq=entry["seq"],
                        session_id=session_id,
                        type_=entry["type"],
                        payload=entry["payload"],
                        message_id=entry.get("message_id"),
                        agent_id=entry.get("agent_id"),
                        parent_agent_id=entry.get("parent_agent_id"),
                    ),
                    seq=entry["seq"],
                )
                last_seq = max(last_seq, entry["seq"])
                last_heartbeat = time.monotonic()
                if entry["type"] in _TERMINAL_EVENT_TYPES:
                    yield _format_frame("meta:completed", {"last_seq": last_seq}, seq=None)
                    return
    finally:
        try:
            loop.run_until_complete(tail_client.aclose())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /api/chat/messages
# ---------------------------------------------------------------------------


@chat_sse_bp.route("/api/chat/messages", methods=["POST", "OPTIONS"])
@require_permission("chat", "write")
def post_message(user_id: str):
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    query = body.get("query")
    if not session_id or not query:
        return jsonify({"error": "session_id and query required"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "no org context"}), 403

    # Same Redis-backed per-user limit the WS receive loop enforces
    # (main_chatbot.py:1106). Without this, SSE was a free path for
    # unbounded Celery enqueue. Fail-open inside is_allowed() if Redis
    # is unreachable.
    if not chat_rate_is_allowed(user_id):
        logger.warning("[chat_sse] rate limit exceeded for user")
        return jsonify({"error": "rate_limited"}), 429

    message_id = str(uuid.uuid4())
    mode = body.get("mode") or "ask"
    model = body.get("model")
    # The frontend may send an empty string when no provider is selected.
    # State.provider_preference is Optional[List[str]] — coerce strings to a
    # list (or None for empty) so pydantic validation doesn't reject the run.
    raw_provider_preference = body.get("provider_preference")
    if isinstance(raw_provider_preference, str):
        cleaned = raw_provider_preference.strip()
        provider_preference = [cleaned] if cleaned else None
    elif isinstance(raw_provider_preference, list):
        provider_preference = [p for p in raw_provider_preference if isinstance(p, str) and p.strip()] or None
    else:
        provider_preference = None
    selected_project_id = body.get("selected_project_id")
    attachments = body.get("attachments") or []
    ui_state = body.get("ui_state")
    # The RCA toggle sets trigger_rca=True; main_chatbot reads the same field
    # on the WS path. Coerce to bool so a stray "true" string doesn't slip in.
    trigger_rca = body.get("trigger_rca") is True

    # Persist user_message + assistant_started so SSE has something to replay
    # immediately after this POST returns. assistant_started also stamps
    # chat_sessions.active_stream_id = "{session_id}:{message_id}" via
    # record_event, which is the row the SSE GET endpoint reads to bind to
    # the stream the worker will write to.
    async def _persist() -> None:
        # Both writes share the same session_id and message_id so they can
        # land in either seq order; the SSE consumer materializes user vs
        # assistant rows independently. Run in parallel — saves one DB
        # round-trip on the POST handler's wall clock.
        await asyncio.gather(
            record_event(
                session_id=session_id,
                org_id=org_id,
                type="user_message",
                payload={"text": query, "mode": mode},
                message_id=message_id,
                agent_id="user",
            ),
            record_event(
                session_id=session_id,
                org_id=org_id,
                type="assistant_started",
                payload={
                    "mode": mode,
                    "model": model,
                    "provider_preference": provider_preference,
                },
                message_id=message_id,
                agent_id="main",
            ),
        )

    try:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_persist())
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            loop.close()
    except Exception as e:
        logger.warning("[chat_sse] post_message: persist failed: %s", e)
        return jsonify({"error": "persist_failed"}), 500

    # Spawn the workflow on a Celery worker. The worker tails the same
    # session_id and writes chat_events that the SSE GET endpoint streams.
    try:
        from chat.background.task import run_background_chat

        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=query,
            trigger_metadata={"source": "chat_sse"},
            provider_preference=provider_preference,
            incident_id=None,  # interactive chat, not RCA
            send_notifications=False,
            mode=mode,
            message_id=message_id,
            model=model,
            selected_project_id=selected_project_id,
            attachments=attachments,
            ui_state=ui_state,
            is_interactive=True,
            trigger_rca=trigger_rca,
        )
    except Exception as e:
        logger.error("[chat_sse] post_message: enqueue failed: %s", e)
        return jsonify({"error": "enqueue_failed"}), 500

    return (
        jsonify({
            "message_id": message_id,
            "stream_url": f"/api/chat/stream?session_id={session_id}",
            "session_id": session_id,
        }),
        202,
    )


# ---------------------------------------------------------------------------
# POST /api/chat/cancel
# ---------------------------------------------------------------------------


@chat_sse_bp.route("/api/chat/cancel", methods=["POST", "OPTIONS"])
@require_permission("chat", "write")
def post_cancel(user_id: str):
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "no org context"}), 403

    message_id = body.get("message_id")

    async def _do_cancel() -> tuple[str, Optional[str], int]:
        # 1. If this session backs a running incident RCA, route through
        #    cancel_rca_for_incident — it owns the strict flip → event →
        #    publish → revoke sequence the DB row is keyed off of.
        rca_handled = await asyncio.to_thread(
            _maybe_cancel_rca_for_session, session_id, user_id
        )

        # 2. Resolve message_id from active_stream_id if not supplied.
        resolved_mid = message_id
        if not resolved_mid:
            try:
                active = await get_active_stream_id(session_id=session_id, org_id=org_id)
                if active and ":" in active:
                    _, resolved_mid = active.split(":", 1)
            except Exception as e:
                logger.warning("[chat_sse] cancel: active lookup failed: %s", e)

        # 3. Record the terminal event (idempotent — partial UNIQUE returns
        #    seq=0 if cancel_rca_for_incident or the workflow already finalized
        #    this message).
        if resolved_mid:
            try:
                await record_event(
                    session_id=session_id,
                    org_id=org_id,
                    type="assistant_interrupted",
                    payload={"reason": "user_cancelled"},
                    message_id=resolved_mid,
                    agent_id="main",
                )
            except Exception as e:
                logger.warning("[chat_sse] cancel: record failed: %s", e)

        # 4. Publish on the cooperative cancel channel for the WS / multi-agent
        #    listeners. Best-effort. Skip if the RCA path already handled it.
        if not rca_handled:
            try:
                client = await get_async_redis()
                if client is not None:
                    try:
                        payload = json.dumps({"message_id": resolved_mid})
                        await client.publish(cancel_channel(session_id), payload)
                        await client.publish(wake_channel(session_id), "1")
                    finally:
                        try:
                            await client.aclose()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("[chat_sse] cancel: redis publish failed: %s", e)

        if not resolved_mid:
            return ("no_active_turn", None, 202)
        return ("cancelled", resolved_mid, 202)

    try:
        loop = asyncio.new_event_loop()
        try:
            status, mid, code = loop.run_until_complete(_do_cancel())
        finally:
            loop.close()
    except Exception as e:
        logger.warning("[chat_sse] cancel: failed: %s", e)
        return jsonify({"error": "cancel_failed"}), 500

    body_out: dict = {"status": status}
    if mid:
        body_out["message_id"] = mid
    return jsonify(body_out), code


def _maybe_cancel_rca_for_session(session_id: str, user_id: str) -> bool:
    """If this session backs a running incident RCA, hand off to
    cancel_rca_for_incident which owns the atomic flip + event +
    publish + revoke sequence. Returns True if it handled the cancel.
    """
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT incident_id FROM chat_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
        incident_id = row[0] if row and row[0] else None
    except Exception as e:
        logger.warning("[chat_sse] cancel: session lookup failed: %s", e)
        return False
    if not incident_id:
        return False
    try:
        from chat.background.task import cancel_rca_for_incident
        return bool(cancel_rca_for_incident(str(incident_id), user_id))
    except Exception as e:
        logger.warning("[chat_sse] cancel: cancel_rca_for_incident failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# POST /api/chat/confirmations
# ---------------------------------------------------------------------------


@chat_sse_bp.route("/api/chat/confirmations", methods=["POST", "OPTIONS"])
@require_permission("chat", "write")
def post_confirmation(user_id: str):
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    confirmation_id = body.get("confirmation_id")
    response = body.get("response")
    if not session_id or not confirmation_id or response not in ("approve", "decline"):
        return jsonify({"error": "session_id, confirmation_id, response (approve|decline) required"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "no org context"}), 403

    # The actual confirmation resolution (mapping confirmation_id → execute|cancel)
    # is owned by infrastructure_confirmation.resolve_confirmation. Here we just
    # publish to the per-session pub/sub so the workflow's listener picks it up.
    async def _publish_confirmation() -> bool:
        client = await get_async_redis()
        if client is None:
            return False
        try:
            payload = json.dumps({
                "confirmation_id": confirmation_id,
                "response": response,
            })
            await client.publish(f"chat:confirm:{session_id}", payload)
            await client.publish(wake_channel(session_id), "1")
            return True
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    published = False
    try:
        loop = asyncio.new_event_loop()
        try:
            published = loop.run_until_complete(_publish_confirmation())
        finally:
            loop.close()
    except Exception as e:
        logger.warning("[chat_sse] confirmations: publish failed: %s", e)

    if not published:
        return jsonify({"error": "confirmation bus unavailable"}), 503
    return jsonify({"status": "accepted"}), 202


# ---------------------------------------------------------------------------
# POST /api/chat/direct-tool
# ---------------------------------------------------------------------------


@chat_sse_bp.route("/api/chat/direct-tool", methods=["POST", "OPTIONS"])
@require_permission("chat", "write")
def post_direct_tool(user_id: str):
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    tool_call_payload: Any = body.get("tool_call_payload")
    if not session_id or not tool_call_payload:
        return jsonify({"error": "session_id and tool_call_payload required"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "no org context"}), 403

    # Direct-tool execution is dispatched via the Redis bus to whichever worker
    # owns this session's runtime; the executor lives in
    # chat.backend.agent.utils.direct_tool_dispatch.dispatch_direct_tool_call.
    async def _publish_direct_tool() -> bool:
        client = await get_async_redis()
        if client is None:
            return False
        try:
            await client.publish(
                f"chat:direct_tool:{session_id}",
                json.dumps(tool_call_payload, default=str),
            )
            await client.publish(wake_channel(session_id), "1")
            return True
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    published = False
    try:
        loop = asyncio.new_event_loop()
        try:
            published = loop.run_until_complete(_publish_direct_tool())
        finally:
            loop.close()
    except Exception as e:
        logger.warning("[chat_sse] direct-tool: publish failed: %s", e)

    if not published:
        return jsonify({"error": "direct-tool bus unavailable"}), 503
    return jsonify({"status": "accepted"}), 202


# Backwards-compat alias used in main_compute.py imports.
bp = chat_sse_bp
