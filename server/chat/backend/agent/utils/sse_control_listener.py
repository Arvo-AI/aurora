"""SSE control listener.

Bridges the SSE-side POST endpoints (``/api/chat/confirmations``,
``/api/chat/direct-tool``) into the same in-process plumbing the WS path uses:

  * ``chat:confirm:{session_id}`` → ``resolve_confirmation`` (sets the result
    on the pending-confirmation map ``wait_for_user_confirmation`` polls)
  * ``chat:direct_tool:{session_id}`` → ``dispatch_direct_tool_call`` (runs
    the tool and emits the ``tool_call_result`` chat_event)

Started by the workflow before the first tool call; stopped on terminal
event (assistant_finalized / interrupted / failed).

Idempotent. Best-effort: a Redis flap must not break the workflow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)


async def listen_for_session_controls(
    *,
    session_id: str,
    user_id: str,
    stop_event: asyncio.Event,
    mode: str = "agent",
    provider_preference: Optional[list] = None,
    selected_project_id: Optional[str] = None,
) -> None:
    """Subscribe to per-session control channels until ``stop_event`` is set.

    Runs as an asyncio task spawned by the workflow. Returns when the stop
    event fires; the caller is responsible for awaiting the task.
    """
    from utils.redis.redis_stream_bus import get_async_redis

    confirm_channel = f"chat:confirm:{session_id}"
    direct_tool_channel = f"chat:direct_tool:{session_id}"

    client = await get_async_redis()
    if client is None:
        logger.warning(
            "[sse_control_listener] redis unavailable; SSE confirmations/direct-tool will not flow for session %s",
            session_id,
        )
        return

    # Track confirmation_ids we've already handled so a duplicate publish
    # is a no-op (logged INFO).
    seen_confirmation_ids: Set[str] = set()

    pubsub = None
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(confirm_channel, direct_tool_channel)
        logger.info(
            "[sse_control_listener] subscribed (session=%s, confirm=%s, direct_tool=%s)",
            session_id, confirm_channel, direct_tool_channel,
        )

        stop_task = asyncio.create_task(stop_event.wait())
        try:
            while not stop_event.is_set():
                msg_task = asyncio.create_task(pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                ))
                done, _pending = await asyncio.wait(
                    {msg_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    msg_task.cancel()
                    try:
                        await msg_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    break
                try:
                    message = msg_task.result()
                except Exception as e:
                    logger.warning("[sse_control_listener] get_message error: %s", e)
                    continue
                if not message:
                    continue

                channel = _decode(message.get("channel"))
                data = _decode(message.get("data"))
                if not data:
                    continue

                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as e:
                    logger.warning("[sse_control_listener] bad payload on %s: %s", channel, e)
                    continue

                if channel == confirm_channel:
                    await _handle_confirmation(payload, user_id, session_id, seen_confirmation_ids)
                elif channel == direct_tool_channel:
                    await _handle_direct_tool(
                        payload, user_id, session_id,
                        mode=mode,
                        provider_preference=provider_preference,
                        selected_project_id=selected_project_id,
                    )
        finally:
            if not stop_task.done():
                stop_task.cancel()
                try:
                    await stop_task
                except (asyncio.CancelledError, Exception):
                    pass
    except Exception as e:
        logger.warning("[sse_control_listener] terminated for session %s: %s", session_id, e)
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(confirm_channel, direct_tool_channel)
                await pubsub.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass
        logger.info(
            "[sse_control_listener] stopped (session=%s)", session_id,
        )


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value)


async def _handle_confirmation(
    payload: dict,
    user_id: str,
    session_id: str,
    seen: Set[str],
) -> None:
    from utils.cloud.infrastructure_confirmation import (
        normalize_decision,
        resolve_confirmation,
    )

    confirmation_id = payload.get("confirmation_id")
    raw_response = payload.get("response") or payload.get("decision")
    if not confirmation_id:
        logger.warning("[sse_control_listener] confirmation missing id: %s", payload)
        return
    if confirmation_id in seen:
        logger.info(
            "[sse_control_listener] duplicate confirmation %s on session %s; dropping",
            confirmation_id, session_id,
        )
        return

    decision = normalize_decision(raw_response)
    if not decision:
        logger.warning(
            "[sse_control_listener] confirmation %s has unrecognized response %r; dropping",
            confirmation_id, raw_response,
        )
        return

    seen.add(confirmation_id)
    resolved = resolve_confirmation(
        confirmation_id=confirmation_id,
        decision=decision,
        user_id=user_id,
        session_id=session_id,
    )
    if not resolved:
        logger.info(
            "[sse_control_listener] resolve_confirmation returned False (cid=%s)",
            confirmation_id,
        )


async def _handle_direct_tool(
    payload: dict,
    user_id: str,
    session_id: str,
    *,
    mode: str,
    provider_preference: Optional[list],
    selected_project_id: Optional[str],
) -> None:
    from chat.backend.agent.utils.direct_tool_dispatch import (
        dispatch_direct_tool_call,
    )

    if not isinstance(payload, dict) or "tool_name" not in payload:
        logger.warning(
            "[sse_control_listener] direct_tool payload missing tool_name: %s", payload,
        )
        return
    try:
        outcome = await dispatch_direct_tool_call(
            payload=payload,
            user_id=user_id,
            session_id=session_id,
            mode=mode,
            provider_preference=provider_preference,
            selected_project_id=selected_project_id,
        )
    except Exception as e:
        logger.error(
            "[sse_control_listener] dispatch_direct_tool_call raised "
            "(user=%s session=%s mode=%s tool=%s): %s",
            user_id, session_id, mode, payload.get("tool_name"), e,
        )
        return
    logger.info(
        "[sse_control_listener] direct_tool %s -> %s",
        outcome.tool_name, outcome.status,
    )
