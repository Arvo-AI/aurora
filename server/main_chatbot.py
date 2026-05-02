import logging
# Configure logging first, before importing any modules - INFO level for cleaner logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# Get module logger
logger = logging.getLogger(__name__)

# Reduce verbosity of specific noisy loggers
logging.getLogger("langchain").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)  
logging.getLogger("weaviate").setLevel(logging.WARNING)
logging.getLogger("redis").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)

import json
import time
import uuid
import os
import jwt as pyjwt
from utils.kubectl.agent_ws_handler import handle_kubectl_agent
from datetime import datetime
from dotenv import load_dotenv
from typing import Optional
from urllib.parse import parse_qs

# Load environment variables
load_dotenv()

import asyncio

# Strong references for fire-and-forget tasks so they aren't GC'd before completion.
_background_tasks: "set[asyncio.Task]" = set()
from langchain_core.messages import AIMessageChunk, HumanMessage, AIMessage
import websockets
import logging

from chat.backend.agent.agent import Agent
from chat.backend.agent.db import PostgreSQLClient
from chat.backend.agent.utils.state import State
from chat.backend.agent.workflow import Workflow
from chat.backend.agent.weaviate_client import WeaviateClient
from chat.backend.agent.utils.llm_context_manager import LLMContextManager
from chat.backend.agent.utils.chat_context_manager import ChatContextManager
from utils.db.connection_pool import db_pool
from utils.billing.billing_cache import update_api_cost_cache_async, get_cached_api_cost
from utils.billing.billing_utils import get_api_cost
from utils.terraform.terraform_cleanup import cleanup_terraform_directory
from utils.cloud.infrastructure_confirmation import handle_websocket_confirmation_response
from chat.backend.agent.tools.cloud_tools import set_user_context
from utils.text.text_utils import clean_markdown
from utils.internal.api_handler import handle_http_request
from utils.auth.stateless_auth import validate_user_exists, get_org_id_for_user, set_rls_context
from utils.rate_limit.chat_rate import is_allowed as chat_rate_is_allowed

_INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")
_AURORA_ENV = os.getenv("AURORA_ENV", "production")

if not _INTERNAL_API_SECRET:
    if _AURORA_ENV == "dev":
        logger.warning(
            "INTERNAL_API_SECRET is not set (AURORA_ENV='dev'). "
            "WebSocket token authentication is disabled — acceptable for local development only."
        )
    else:
        raise RuntimeError(
            "FATAL: INTERNAL_API_SECRET is not set and AURORA_ENV='%s' (non-dev). "
            "Refusing to start without authentication secrets in production." % _AURORA_ENV
        )

def _validate_ws_token(websocket) -> dict | None:
    """Extract and validate a JWT from the WebSocket handshake query string.

    Returns the decoded payload dict on success, or None if no token is present or validation fails.
    """
    if not _INTERNAL_API_SECRET:
        return None

    try:
        raw_path = str(websocket.request.path)
        if "?" not in raw_path:
            return None

        qs = parse_qs(raw_path.split("?", 1)[1])
        token_list = qs.get("token")
        if not token_list:
            return None

        token = token_list[0]
        ws_key = _INTERNAL_API_SECRET + "aurora:ws-token-signing"
        payload = pyjwt.decode(
            token,
            ws_key,
            algorithms=["HS256"],
            audience="chatbot-ws",
            options={"require": ["exp", "aud"]},
        )

        jti = payload.get("jti")
        if not jti:
            logger.warning("WebSocket token missing required jti claim")
            return None

        from utils.cache.redis_client import get_redis_client
        r = get_redis_client()
        if not r:
            logger.error("Redis unavailable for jti replay check, rejecting token jti=%s", jti)
            return None
        if not r.set(f"ws:jti:{jti}", "1", nx=True, ex=120):
            logger.warning("WebSocket token replay detected: jti=%s", jti)
            return None

        return payload
    except pyjwt.ExpiredSignatureError:
        logger.warning("WebSocket token expired")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.warning("WebSocket token invalid: %s", e)
        return None
    except Exception as e:
        logger.warning("Unexpected error validating WS token: %s", e)
        return None


def _normalize_mode(mode: Optional[str]) -> str:
    return (mode or "agent").strip().lower()


# Note: Removed per-user concurrency limitation to enable true multi-session support
# Concurrent sessions are now safely isolated via session-specific terraform directories

# Deployment update listener removed

async def _ws_reject(websocket, text: str, *, close_reason: str = ""):
    """Send an error frame and optionally close the WebSocket."""
    await websocket.send(json.dumps({"type": "error", "data": {"text": text}}))
    if close_reason:
        await websocket.close(code=1008, reason=close_reason)


def _warm_user_caches(user_id: str):
    """Kick off background tasks that pre-warm per-user caches."""
    _cost_warm_task = asyncio.create_task(update_api_cost_cache_async(user_id))
    _background_tasks.add(_cost_warm_task)
    _cost_warm_task.add_done_callback(_background_tasks.discard)
    logger.info(f"Started preemptive API cost cache update for user {user_id}")

    try:
        from chat.backend.agent.tools.mcp_preloader import preload_user_tools
        preload_user_tools(user_id)
        logger.info(f"Triggered MCP preload for user {user_id} on connection")
    except Exception as e:
        logger.debug(f"Failed to trigger MCP preload on connection: {e}")

    try:
        from chat.backend.agent.tools.mcp_preloader import update_user_activity
        update_user_activity(user_id)
        logger.debug(f"Updated MCP preloader activity for user {user_id}")
    except Exception as e:
        logger.debug(f"Failed to update MCP preloader activity: {e}")


async def handle_init(data, websocket, current_user_id, deployment_listener_task):
    """Handle connection initialization message and return updated state.

    If the connection was already authenticated via a handshake token,
    current_user_id is pre-set and we skip the DB validation step.
    """

    user_id = data.get('user_id')

    if user_id and current_user_id and current_user_id != user_id:
        logger.warning(
            f"WebSocket init rejected: token user {current_user_id!r} "
            f"does not match init user_id {user_id!r}"
        )
        await _ws_reject(websocket, "Authentication failed: user identity mismatch.", close_reason="User identity mismatch")
        return current_user_id, deployment_listener_task

    if user_id and not current_user_id:
        if _INTERNAL_API_SECRET:
            logger.warning(
                f"WebSocket init rejected: legacy auth attempted while INTERNAL_API_SECRET is configured (user_id={user_id!r})"
            )
            await _ws_reject(websocket, "Authentication failed: token-based auth required.", close_reason="Token-based auth required")
            return current_user_id, deployment_listener_task

        if not validate_user_exists(user_id):
            logger.warning(f"WebSocket init rejected: invalid user_id {user_id!r}")
            await _ws_reject(websocket, "Authentication failed: invalid user identity.")
            return current_user_id, deployment_listener_task

        logger.info(f"Initializing connection for user {user_id}")
        current_user_id = user_id

        _warm_user_caches(user_id)

        if deployment_listener_task:
            deployment_listener_task.cancel()
            try:
                await deployment_listener_task
            except asyncio.CancelledError:
                pass

        logger.info(f"Started deployment listener for user {user_id}")

    return current_user_id, deployment_listener_task
def process_attachments_and_append_to_question(question, attachments):
    """Process incoming attachments, log details, and append a human-readable summary to the question."""
    if attachments:
        logger.info(f"Received {len(attachments)} file attachment(s)")
        # Log attachment details for debugging
        for i, attachment in enumerate(attachments):
            filename = attachment.get('filename', 'unknown')
            file_type = attachment.get('file_type', '')
            is_server_path = attachment.get('is_server_path', False)
            server_path = attachment.get('server_path', '')
            has_file_data = bool(attachment.get('file_data', ''))
            logger.info(f"  Attachment {i}: {filename} (type: {file_type}, server_path: {is_server_path}, path: {server_path}, has_data: {has_file_data})")

        # Process attachments and append to question
        attachment_context = []
        for attachment in attachments:
            filename = attachment.get('filename', 'unknown')
            file_type = attachment.get('file_type', '')
            file_data = attachment.get('file_data', '')
            server_path = attachment.get('server_path', '')
            is_server_path = attachment.get('is_server_path', False)

            if is_server_path and server_path:
                # Handle server-side file paths (e.g., uploaded zip files)
                if file_type in ['application/zip', 'application/x-zip-compressed']:
                    attachment_context.append(f"[Attached ZIP file: {filename} (server path: {server_path})]")
                    logger.info(f"Zip file attachment with server path: {server_path}")
                else:
                    attachment_context.append(f"[Attached file: {filename} (server path: {server_path})]")
            elif file_type.startswith('image/'):
                attachment_context.append(f"[Attached image: {filename}]")
            elif file_type == 'application/pdf':
                attachment_context.append(f"[Attached PDF: {filename}]")

        if attachment_context:
            # Append attachment info to the question
            question = question + "\n\n" + "\n".join(attachment_context)
    else:
        logger.info("No file attachments received in this message")

    return question

def create_websocket_sender(websocket, user_id, session_id):
    """Create an async sender callable for tool outputs over the websocket.
    Keeps original single-argument signature expected by tools and the Agent.
    Gracefully handles WebSocket disconnections without stopping workflow.
    """
    
    async def sender(data):
        try:
            # Handle both string and dictionary inputs
            if isinstance(data, str):
                # If it's a string, try to parse as JSON first
                try:
                    await websocket.send(data)  # Send the original string
                except json.JSONDecodeError:
                    # If not JSON, treat as plain text message
                    message_data = {
                        "type": "message",
                        "data": {"text": data}
                    }
                    await websocket.send(json.dumps(message_data))
            else:
                # Original dictionary handling
                await websocket.send(json.dumps(data))
                try:
                    msg_type = data.get('type', 'unknown')
                except Exception:
                    msg_type = 'unknown'
        except websockets.exceptions.ConnectionClosed:
            logger.debug(f"WEBSOCKET: Connection {id(websocket)} closed, continuing workflow in background")
            return False  # Indicate connection lost but don't stop workflow
        except Exception as e:
            logger.warning(f"WEBSOCKET: Error sending tool data via connection {id(websocket)}: {e}")
            return False
        return True

    # The active sender is now bound via the `_active_ws_sender_var` ContextVar
    # in process_workflow_async; no global registry to register against.
    logger.debug(
        "WEBSOCKET: Built sender for connection %s user=%s session=%s",
        id(websocket), user_id, session_id,
    )

    return sender


async def process_workflow_async(wf, state, websocket, user_id, incident_id=None):
    curr_node = "START"
    sent_message_count = 0
    websocket_connected = True
    workflow_timeout = 1800  # 30 minutes max for any workflow
    session_id = getattr(state, 'session_id', 'unknown')

    from chat.backend.agent.utils.persistence.chat_events import (
        set_active_ws_sender,
        reset_active_ws_sender,
    )
    _ws_sender_for_events = None
    if hasattr(wf, 'agent') and getattr(wf.agent, 'websocket_sender', None):
        _ws_sender_for_events = wf.agent.websocket_sender
    _ws_sender_token = set_active_ws_sender(_ws_sender_for_events)

    # Incident thoughts buffered writer (sentence-boundary aware).
    # Shared with the multi-agent path; all mid-run thought persistence flows through this.
    from chat.backend.agent.utils.persistence.incident_thoughts import IncidentThoughtAccumulator

    _thought_accumulator = IncidentThoughtAccumulator(incident_id, agent_id="main")

    def save_incident_thought(content: str, force: bool = False):
        _thought_accumulator.push(content, force=force)


    # Helper to incrementally save streaming chat messages for background chats.
    # Same pattern as save_incident_thought but writes to chat_sessions.messages
    # so the frontend's 2s polling can show partial responses.
    is_background = getattr(state, 'is_background', False)
    accumulated_chat_msg = []
    last_chat_save_time = [time.time()]
    # How many characters of `accumulated_chat_msg` have already been emitted as
    # an `assistant_chunk` chat_event. Each chunk carries only the DELTA since
    # the last emission so the SSE consumer's reducer can append cleanly; the
    # projection writer below stamps the FULL cumulative snapshot.
    last_emitted_len = [0]
    # Honor a pre-seeded message_id (e.g. SSE POST allocated it before
    # enqueuing the Celery task). Falls back to lazy allocation otherwise.
    pre_seeded_mid = getattr(wf, '_active_message_id', None)
    streaming_message_id = [pre_seeded_mid if pre_seeded_mid else None]
    org_id_for_events = getattr(state, 'org_id', None) or (get_org_id_for_user(user_id) if user_id else None)

    def _streaming_message_id() -> str:
        if not streaming_message_id[0]:
            streaming_message_id[0] = str(uuid.uuid4())
            # Expose the active message id on the workflow so the cancel
            # handler in the WS message loop can record assistant_interrupted
            # for this exact message.
            try:
                wf._active_message_id = streaming_message_id[0]
            except Exception:
                pass
        return streaming_message_id[0]

    async def _emit_streaming_event(
        type_: str,
        delta_text: str,
        full_text: str,
        *,
        finalized: bool,
        status: str,
    ) -> None:
        """Emit one chat_event. Token chunks fire async + out-of-order, so the
        projection write is deferred to finalize (`finalized=True`) — otherwise
        a late chunk can clobber the terminal `status='complete'` with
        `'streaming'`. The SSE consumer rebuilds parts[] from chat_events live;
        the projection is the post-finalize snapshot.
        """
        try:
            from chat.backend.agent.utils.persistence.chat_events import (
                record_event,
                upsert_message_projection,
            )
            if not session_id or session_id == 'unknown' or not org_id_for_events:
                return
            mid = _streaming_message_id()
            await record_event(
                session_id=session_id,
                org_id=org_id_for_events,
                type=type_,
                payload={'text': delta_text},
                message_id=mid,
            )
            if finalized:
                await upsert_message_projection(
                    message_id=mid,
                    session_id=session_id,
                    org_id=org_id_for_events,
                    role='assistant',
                    status=status,
                    parts=[{'type': 'text', 'text': full_text}] if full_text else [],
                    finalized=True,
                )
        except Exception as e:
            logger.warning("[chat_events:dual_write_failed] %s", e)

    # Track scheduled emit tasks so the workflow's outer try/finally can drain
    # them before the Celery wrapper's asyncio.run() teardown cancels them.
    # Without this, terminal `assistant_finalized` events emitted as fire-and-
    # forget create_task lose their awaited DB writes (projection + active_stream_id
    # clear) when the loop tears down.
    pending_emit_tasks: set[asyncio.Task] = set()
    wf._pending_emit_tasks = pending_emit_tasks  # exposed so callers can drain

    def _schedule_streaming_event(type_: str, delta_text: str, full_text: str, *, finalized: bool, status: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_emit_streaming_event(type_, delta_text, full_text, finalized=finalized, status=status))
            pending_emit_tasks.add(task)
            task.add_done_callback(pending_emit_tasks.discard)
        except RuntimeError:
            try:
                asyncio.run(_emit_streaming_event(type_, delta_text, full_text, finalized=finalized, status=status))
            except Exception as e:
                logger.warning("[chat_events:dual_write_failed] %s", e)

    async def _emit_tool_event(type_: str, payload: dict) -> None:
        try:
            from chat.backend.agent.utils.persistence.chat_events import record_event
            if not session_id or session_id == 'unknown' or not org_id_for_events:
                return
            await record_event(
                session_id=session_id,
                org_id=org_id_for_events,
                type=type_,
                payload=payload,
            )
        except Exception as e:
            logger.warning("[chat_events:dual_write_failed] %s", e)

    def _schedule_tool_event(type_: str, payload: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_emit_tool_event(type_, payload))
        except RuntimeError:
            try:
                asyncio.run(_emit_tool_event(type_, payload))
            except Exception as e:
                logger.warning("[chat_events:dual_write_failed] %s", e)

    async def _emit_usage_event(type_: str, payload: dict) -> None:
        # message_id pin: SSE bridge tails chat_events:{session_id}:{message_id};
        # without the pin the redis fan-out is skipped and the meter never updates.
        try:
            from chat.backend.agent.utils.persistence.chat_events import record_event
            mid = streaming_message_id[0]
            if not session_id or session_id == 'unknown' or not org_id_for_events or not mid:
                # Surface the drop so missing-message-id usage events are diagnosable.
                logger.debug(
                    "[chat_events:usage] dropping %s (session=%s org=%s mid=%s)",
                    type_, bool(session_id and session_id != 'unknown'),
                    bool(org_id_for_events), bool(mid),
                )
                return
            await record_event(
                session_id=session_id,
                org_id=org_id_for_events,
                type=type_,
                payload=payload,
                message_id=mid,
            )
        except Exception as e:
            logger.warning("[chat_events:dual_write_failed] %s", e)

    def save_streaming_chat_message(content: str, force: bool = False):
        """Persist incremental assistant tokens. ALWAYS emits an `assistant_chunk`
        chat_event so SSE consumers see live streaming. The legacy chat_sessions.messages
        JSONB write is kept only for `is_background=True` (Celery / RCA) callers that
        the polling UI still depends on; SSE-driven turns read from chat_messages.parts[]."""
        if not session_id or session_id == 'unknown':
            return

        if content:
            accumulated_chat_msg.append(content)

        accumulated_text = "".join(accumulated_chat_msg)

        if not accumulated_text:
            return

        current_time = time.time()
        time_since_last = current_time - last_chat_save_time[0]

        should_flush = force or time_since_last >= 1.5 or len(accumulated_text) >= 200
        if not should_flush or (not force and len(accumulated_text) < 30):
            return

        # Emit chat_event chunk first — fires for SSE + WS regardless of is_background.
        # The event payload carries only the new chars since last emission; the
        # projection write inside _emit_streaming_event keeps the cumulative snapshot.
        delta = accumulated_text[last_emitted_len[0]:]
        last_emitted_len[0] = len(accumulated_text)
        _schedule_streaming_event(
            'assistant_chunk',
            delta,
            accumulated_text,
            finalized=False,
            status='streaming',
        )
        last_chat_save_time[0] = current_time

        # Legacy JSONB write — only background path needs it (polling UI fallback).
        if not is_background:
            return

        try:
            from utils.db.connection_pool import db_pool

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[Chatbot:StreamingSave]")
                    cursor.execute(
                        "SELECT messages FROM chat_sessions WHERE id = %s FOR UPDATE",
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return

                    messages = row[0] if row[0] else []
                    if isinstance(messages, str):
                        messages = json.loads(messages)

                    bot_msg = None
                    for msg in reversed(messages):
                        if msg.get("sender") == "bot" and msg.get("_streaming"):
                            bot_msg = msg
                            break

                    if bot_msg:
                        bot_msg["text"] = accumulated_text
                    else:
                        messages.append({
                            "sender": "bot",
                            "text": accumulated_text,
                            "_streaming": True,
                        })

                    cursor.execute(
                        "UPDATE chat_sessions SET messages = %s, updated_at = %s WHERE id = %s",
                        (json.dumps(messages), datetime.now(), session_id),
                    )
                conn.commit()
                logger.debug(f"[BackgroundChat] Saved streaming message for session {session_id}: {len(accumulated_text)} chars")
        except Exception as e:
            logger.error(f"[BackgroundChat] Failed to save streaming chat message: {e}")

    async def flush_streaming_chat_message_awaited():
        # Awaited (vs save_streaming_chat_message's fire-and-forget) so the
        # assistant_chunk's seq is allocated before any tool event scheduled
        # next; the reset opens a fresh text segment for the next AIMessage.
        if accumulated_chat_msg:
            total_len = sum(len(s) for s in accumulated_chat_msg)
            if total_len > last_emitted_len[0]:
                full_text = "".join(accumulated_chat_msg)
                delta = full_text[last_emitted_len[0]:]
                await _emit_streaming_event(
                    'assistant_chunk', delta, full_text,
                    finalized=False, status='streaming',
                )
            accumulated_chat_msg.clear()
            last_emitted_len[0] = 0
        last_chat_save_time[0] = time.time()

    def finalize_streaming_chat_message(remove: bool = False):
        """Finalize the running assistant message.

        Always emits an ``assistant_finalized`` chat_event so the SSE consumer
        can flip status → complete. The legacy chat_sessions.messages JSONB
        cleanup runs only for ``is_background=True`` (polling-UI fallback).

        When *remove=False* (mid-stream), the bot row's ``_streaming`` flag is
        cleared. When *remove=True*, streaming rows are deleted (the workflow
        will write the authoritative UI messages on completion).
        """
        if not session_id or session_id == 'unknown':
            return

        # SSE / chat_events finalize — always run.
        if accumulated_chat_msg:
            final_text = "".join(accumulated_chat_msg)
            delta = final_text[last_emitted_len[0]:]
            _schedule_streaming_event(
                'assistant_finalized',
                delta,
                final_text,
                finalized=True,
                status='complete',
            )
            accumulated_chat_msg.clear()
            last_emitted_len[0] = 0
            # NOTE: do NOT reset streaming_message_id[0] / wf._active_message_id
            # here. The workflow's _emit_chat_events_for_messages runs after
            # finalize and reads wf._active_message_id to keep all events under
            # the same id. Resetting causes a fresh uuid → duplicate finalize.

        # Legacy JSONB cleanup — only background callers care.
        if not is_background:
            return

        try:
            from utils.db.connection_pool import db_pool

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[Chatbot:StreamingFinalize]")
                    cursor.execute(
                        "SELECT messages FROM chat_sessions WHERE id = %s FOR UPDATE",
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return

                    messages = row[0] if row[0] else []
                    if isinstance(messages, str):
                        messages = json.loads(messages)

                    modified = False
                    if remove:
                        original_len = len(messages)
                        messages = [
                            msg for msg in messages
                            if not msg.get("_streaming")
                        ]
                        modified = len(messages) < original_len
                    else:
                        for msg in messages:
                            if msg.get("_streaming"):
                                del msg["_streaming"]
                                modified = True

                    if modified:
                        cursor.execute(
                            "UPDATE chat_sessions SET messages = %s, updated_at = %s WHERE id = %s",
                            (json.dumps(messages), datetime.now(), session_id),
                        )
                        conn.commit()
                        logger.debug(
                            f"[BackgroundChat] Finalized streaming messages for session {session_id} "
                            f"(remove={remove})"
                        )
        except Exception as e:
            logger.error(f"[BackgroundChat] Failed to finalize streaming chat message: {e}")

    # Helper function to send messages via the appropriate sender
    async def send_via_appropriate_sender(message_data):
        nonlocal websocket_connected
        try:
            # Get the Agent's websocket_sender fresh each time (it may have been updated)
            current_agent_sender = None
            if hasattr(wf, 'agent') and hasattr(wf.agent, 'websocket_sender'):
                current_agent_sender = wf.agent.websocket_sender
            
            if current_agent_sender:
                # Use Agent's websocket_sender (which may have been updated)
                success = await current_agent_sender(message_data)
                if not success:
                    websocket_connected = False
                    logger.warning("WEBSOCKET: Agent websocket_sender failed - connection may be closed")
            else:
                # Fallback to original websocket
                await websocket.send(json.dumps(message_data))
        except websockets.exceptions.ConnectionClosed:
            websocket_connected = False
            logger.warning("WEBSOCKET: Connection closed during send - continuing workflow in background")
        except Exception as e:
            logger.warning(f"WEBSOCKET: Error sending message: {e}")
            websocket_connected = False
    
    # Helper to send END status - called on completion or timeout
    async def send_end_status(reason="completed"):
        if websocket_connected:
            final_response = {
                "type": "status",
                "data": {"status": "END"},
                "isComplete": True,
            }
            if hasattr(state, 'session_id') and state.session_id:
                final_response["session_id"] = state.session_id
            try:
                await send_via_appropriate_sender(final_response)
                logger.info(f"Sent END status for session {session_id} (reason: {reason})")
            except Exception as e:
                logger.error(f"Failed to send END status for session {session_id}: {e}")
    
    try:
        # Wrap the workflow stream with timeout to prevent infinite hangs
        async def process_stream():
            nonlocal sent_message_count
            # Track tool calls we've already sent to avoid duplicates
            sent_tool_call_ids = set()
            event_count = 0
            
            try:
                async for event_type, event_data in wf.stream(state):
                    event_count += 1
                    
                    if event_type == "token":
                        # Real-time token streaming from LLM
                        if websocket_connected:
                            token_text = event_data  # event_data is the token string
                            if token_text:
                                msg_response = {
                                    "type": "message",
                                    "data": {
                                        "text": token_text,
                                        "is_chunk": True,
                                        "is_complete": False,
                                        "streaming": True
                                    },
                                }
                                if hasattr(state, 'session_id') and state.session_id:
                                    msg_response["session_id"] = state.session_id

                                await send_via_appropriate_sender(msg_response)
                                # Save tokens incrementally to incident thoughts
                                save_incident_thought(token_text, force=False)
                                save_streaming_chat_message(token_text, force=False)

                    elif event_type == "flush_accumulator":
                        await flush_streaming_chat_message_awaited()

                    elif event_type == "values":
                        # Stub: skip complete-state "values" events.
                        # The real handler below would re-send content already streamed
                        # via the "messages" event path, causing duplicates in the UI / DB.
                        # Keep this branch first so the duplicate-send path is unreachable.
                        logger.debug("[STREAM DEBUG] Received values event (skipped to avoid duplicate sends)")
                        continue

                    elif event_type == "messages":
                        try:
                            msg_chunk, _ = event_data
                            logger.debug(f"[STREAM DEBUG] Successfully unpacked message chunk: type={type(msg_chunk).__name__}, content_length={len(str(getattr(msg_chunk, 'content', '')))}, is AIMessageChunk={isinstance(msg_chunk, AIMessageChunk)}")
                        except (ValueError, TypeError) as unpack_error:
                            logger.error(f"[STREAM ERROR] Failed to unpack message event_data: {unpack_error}, event_data type: {type(event_data)}, event_data: {event_data}")
                            continue
                        
                        logger.info(f"[STREAM DEBUG] Message chunk type: {type(msg_chunk).__name__}, is AIMessageChunk: {isinstance(msg_chunk, AIMessageChunk)}, is AIMessage: {isinstance(msg_chunk, AIMessage)}, websocket_connected: {websocket_connected}, has_content: {bool(getattr(msg_chunk, 'content', None))}")
                        
                        # Handle both AIMessageChunk (streaming) and AIMessage (complete) from langchain
                        if isinstance(msg_chunk, (AIMessageChunk, AIMessage)) and websocket_connected:
                            # Check for tool calls in the message chunk and send them
                            tool_calls = None
                            if hasattr(msg_chunk, 'tool_calls') and msg_chunk.tool_calls:
                                tool_calls = msg_chunk.tool_calls
                            elif hasattr(msg_chunk, 'additional_kwargs') and msg_chunk.additional_kwargs:
                                tool_calls = msg_chunk.additional_kwargs.get('tool_calls', [])
                            
                            # Send tool calls that haven't been sent yet
                            if tool_calls:
                                for tool_call in tool_calls:
                                    tool_call_id = tool_call.get('id') or tool_call.get('tool_call_id')
                                    if tool_call_id and tool_call_id not in sent_tool_call_ids:
                                        sent_tool_call_ids.add(tool_call_id)
                                        tool_name = tool_call.get('function', {}).get('name', 'unknown')
                                        tool_args = tool_call.get('function', {}).get('arguments', '{}')
                                        
                                        try:
                                            tool_input = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                        except:
                                            tool_input = tool_args
                                        
                                        tool_call_msg = {
                                            "type": "tool_call",
                                            "data": {
                                                "tool_name": tool_name,
                                                "input": tool_input,
                                                "status": "running",
                                                "timestamp": datetime.now().isoformat(),
                                                "tool_call_id": tool_call_id
                                            }
                                        }
                                        if hasattr(state, 'session_id') and state.session_id:
                                            tool_call_msg["session_id"] = state.session_id
                                        await send_via_appropriate_sender(tool_call_msg)
                                        logger.debug(f"Sent tool call via streaming: {tool_name} (id: {tool_call_id})")
                                        # NOTE: tool_call_started chat_events emission moved to ToolContextCapture
                                        # (single source of truth, agent_id-aware, covers sub-agents).

                            # Send message content if present
                            chunk_content = getattr(msg_chunk, 'content', '')
                            logger.info(f"[STREAM DEBUG] Checking content: chunk_content length={len(str(chunk_content))}, empty={chunk_content == ''}, type={type(chunk_content)}")
                            if chunk_content and str(chunk_content).strip() != "":
                                # Split large chunks into smaller pieces for smoother streaming
                                # This ensures thoughts appear progressively even when LLM sends large chunks
                                import re
                                raw_content = msg_chunk.content
                                cleaned_content = re.sub(r'\s{3,}', ' ', raw_content)  # Replace 3+ consecutive whitespace with single space
                                cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content)  # Replace 3+ newlines with double newline
                                
                                if cleaned_content:  # Only send non-empty chunks after cleaning
                                    # Split large chunks into sentences for smoother streaming
                                    sentences = re.split(r'([.!?]\s+)', cleaned_content)
                                    current_chunk = ""
                                    
                                    for i, part in enumerate(sentences):
                                        current_chunk += part
                                        # Send when we hit sentence boundary or chunk is getting large
                                        is_sentence_boundary = bool(re.match(r'[.!?]\s+$', part))
                                        if is_sentence_boundary or len(current_chunk) > 100:
                                            if current_chunk.strip():
                                                msg_response = {
                                                    "type": "message",
                                                    "data": {
                                                        "text": current_chunk,
                                                        "is_chunk": True,
                                                        "is_complete": False,
                                                        "streaming": True
                                                    },
                                                }
                                                if hasattr(state, 'session_id') and state.session_id:
                                                    msg_response["session_id"] = state.session_id
                                                logger.debug(f"[STREAM SEND] Sending split chunk ({len(current_chunk)} chars)")
                                                await send_via_appropriate_sender(msg_response)
                                                # Save to incident thoughts incrementally
                                                save_incident_thought(current_chunk, force=False)
                                                save_streaming_chat_message(current_chunk, force=False)
                                                current_chunk = ""
                                    
                                    # Send any remaining content
                                    if current_chunk.strip():
                                        msg_response = {
                                            "type": "message",
                                            "data": {
                                                "text": current_chunk,
                                                "is_chunk": True,
                                                "is_complete": False,
                                                "streaming": True
                                            },
                                        }
                                        if hasattr(state, 'session_id') and state.session_id:
                                            msg_response["session_id"] = state.session_id
                                        logger.debug(f"[STREAM SEND] Sending final split chunk ({len(current_chunk)} chars)")
                                        await send_via_appropriate_sender(msg_response)
                                        save_incident_thought(current_chunk, force=False)
                                        save_streaming_chat_message(current_chunk, force=False)
                    elif event_type == "values":
                        # Handle "values" events - complete messages from state updates
                        if hasattr(event_data, 'messages') and event_data.messages and websocket_connected:
                            current_message_count = len(event_data.messages)
                            if current_message_count > sent_message_count:
                                for i in range(sent_message_count, current_message_count):
                                    message = event_data.messages[i]
                                    if isinstance(message, AIMessage):
                                        # Send tool calls if present
                                        tool_calls = None
                                        if hasattr(message, 'tool_calls') and message.tool_calls:
                                            tool_calls = message.tool_calls
                                        elif hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                                            tool_calls = message.additional_kwargs.get('tool_calls', [])
                                        
                                        if tool_calls:
                                            for tool_call in tool_calls:
                                                tool_call_id = tool_call.get('id') or tool_call.get('tool_call_id')
                                                if tool_call_id and tool_call_id not in sent_tool_call_ids:
                                                    sent_tool_call_ids.add(tool_call_id)
                                                    tool_name = tool_call.get('function', {}).get('name') or tool_call.get('name', 'unknown')
                                                    tool_args = tool_call.get('function', {}).get('arguments') or tool_call.get('args', '{}')
                                                    
                                                    try:
                                                        tool_input = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                                    except:
                                                        tool_input = tool_args
                                                    
                                                    tool_call_msg = {
                                                        "type": "tool_call",
                                                        "data": {
                                                            "tool_name": tool_name,
                                                            "input": tool_input,
                                                            "status": "running",
                                                            "timestamp": datetime.now().isoformat(),
                                                            "tool_call_id": tool_call_id
                                                        }
                                                    }
                                                    if hasattr(state, 'session_id') and state.session_id:
                                                        tool_call_msg["session_id"] = state.session_id
                                                    await send_via_appropriate_sender(tool_call_msg)
                                                    logger.debug(f"Sent tool call from values event: {tool_name} (id: {tool_call_id})")
                                                    # NOTE: tool_call_started chat_events emission moved to ToolContextCapture
                                                    # (single source of truth, agent_id-aware, covers sub-agents).
                                        
                                        # Send message content if present
                                        if hasattr(message, 'content') and message.content:
                                            complete_msg_response = {
                                                "type": "message",
                                                "data": {"text": message.content},
                                            }
                                            if hasattr(state, 'session_id') and state.session_id:
                                                complete_msg_response["session_id"] = state.session_id
                                            await send_via_appropriate_sender(complete_msg_response)
                                            # Force save accumulated thought before starting new message
                                            save_incident_thought("", force=True)
                                            save_incident_thought(message.content, force=False)
                                            # Finalize current streaming message and start fresh.
                                            # Reset both the buffer and the delta-position cursor;
                                            # otherwise the next chunk's `delta` is sliced against
                                            # the old length and emits incorrect text.
                                            save_streaming_chat_message("", force=True)
                                            finalize_streaming_chat_message()
                                            accumulated_chat_msg.clear()
                                            last_emitted_len[0] = 0
                                            save_streaming_chat_message(message.content, force=False)
                                sent_message_count = current_message_count

                    elif event_type == "usage_update":
                        if websocket_connected:
                            usage_msg = {
                                "type": "usage_update",
                                "data": event_data,
                            }
                            if hasattr(state, 'session_id') and state.session_id:
                                usage_msg["session_id"] = state.session_id
                            await send_via_appropriate_sender(usage_msg)
                        await _emit_usage_event("usage_update", event_data)

                    elif event_type == "usage_final":
                        if websocket_connected:
                            usage_msg = {
                                "type": "usage_final",
                                "data": event_data,
                            }
                            if hasattr(state, 'session_id') and state.session_id:
                                usage_msg["session_id"] = state.session_id
                            await send_via_appropriate_sender(usage_msg)
                        await _emit_usage_event("usage_final", event_data)
                        logger.info(
                            f"[USAGE FINAL] {event_data.get('model')}: "
                            f"{event_data.get('input_tokens', 0)}+{event_data.get('output_tokens', 0)} tokens, "
                            f"${event_data.get('estimated_cost', 0):.6f}"
                        )

                    else:
                        logger.warning(f"[STREAM DEBUG] Unhandled event type: {event_type}, data type: {type(event_data).__name__}")
                
                logger.info(f"[STREAM DEBUG] Stream completed. Total events: {event_count}")
            except Exception as stream_error:
                logger.error(f"[STREAM ERROR] Error in process_stream for session {session_id}: {stream_error}", exc_info=True)
                raise
        
        # Execute workflow with timeout
        await asyncio.wait_for(process_stream(), timeout=workflow_timeout)
        
        # CRITICAL: Wait for any ongoing tool calls to complete before marking as done
        if hasattr(wf, '_wait_for_ongoing_tool_calls'):
            await wf._wait_for_ongoing_tool_calls()
        logger.info(f"Workflow completed normally for session {session_id} - WebSocket connected: {websocket_connected}")
        
        # Force save any remaining accumulated thought
        save_incident_thought("", force=True)

        # Finalize streaming: remove temporary streaming messages before the
        # authoritative UI messages are written by _append_new_turn_ui_messages
        finalize_streaming_chat_message(remove=True)
        
        await send_end_status("completed")
        
    except asyncio.TimeoutError:
        logger.error(f"Workflow timeout after {workflow_timeout}s for session {session_id}")
        finalize_streaming_chat_message(remove=True)
        if websocket_connected:
            timeout_msg = {
                "type": "error",
                "data": {"text": "Workflow timeout - the operation may have completed but the response took too long. Please check your resources manually."},
            }
            if hasattr(state, 'session_id') and state.session_id:
                timeout_msg["session_id"] = state.session_id
            await send_via_appropriate_sender(timeout_msg)
        await send_end_status("timeout")
        
    except Exception as e:
        logger.error(f"Error in workflow processing for session {session_id}: {e}", exc_info=True)
        finalize_streaming_chat_message(remove=True)
        if websocket_connected:
            error_msg = {
                "type": "error",
                "data": {"text": "A workflow error occurred. Please try again."},
            }
            if hasattr(state, 'session_id') and state.session_id:
                error_msg["session_id"] = state.session_id
            await send_via_appropriate_sender(error_msg)
        await send_end_status("error")
    finally:
        # Drain pending emit tasks so terminal `assistant_finalized` events
        # complete their DB writes before the Celery wrapper's asyncio.run()
        # teardown cancels them.
        if pending_emit_tasks:
            try:
                await asyncio.gather(*pending_emit_tasks, return_exceptions=True)
            except Exception as e:
                logger.warning("[chat_events:drain_pending_failed] %s", e)
            pending_emit_tasks.clear()
        reset_active_ws_sender(_ws_sender_token)

    # Handle API cost tracking - always execute regardless of WebSocket status
    if user_id:
        try:
            # Update API cost cache after workflow completion to capture new usage
            # This ensures that any LLM usage from this workflow is immediately reflected
            _cost_update_task = asyncio.create_task(update_api_cost_cache_async(user_id))
            _background_tasks.add(_cost_update_task)
            _cost_update_task.add_done_callback(_background_tasks.discard)
            logger.debug(f"Triggered post-request API cost update for user {user_id}")

            is_cached, total_cost = get_cached_api_cost(user_id)

            if not is_cached:
                total_cost = get_api_cost(user_id)
                logger.info(f"API cost tracking for user {user_id}: ${total_cost:.2f}")

            # Log API usage information (for cost tracking purposes)
            logger.info(f"User {user_id} API cost: ${total_cost:.2f} (WebSocket connected: {websocket_connected})")

            # Send usage info if WebSocket is connected (for cost tracking display)
            if websocket_connected:
                usage_info_response = {
                    "type": "usage_info",
                    "data": {
                        "total_cost": round(total_cost, 2),
                    },
                }
                await send_via_appropriate_sender(usage_info_response)
        except Exception as e:
            logger.error(f"Error tracking API costs: {e}")
    
    # Workflow always completes here - messages are saved by the workflow.stream() method
    logger.info(f"Background workflow processing completed for session {getattr(state, 'session_id', 'unknown')}")

async def handle_connection(websocket) -> None:
    """Handle a single WebSocket connection."""
    auth_header = websocket.request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        await handle_kubectl_agent(websocket)
        return
    
    client_id = id(websocket)
    logger.info(f"New client connected. ID: {client_id}")

    # Validate JWT token from handshake query string
    token_payload = _validate_ws_token(websocket)
    token_user_id = None
    if token_payload:
        token_user_id = token_payload.get("userId")
        logger.info(f"WebSocket authenticated via token: user={token_user_id}")

        if not token_user_id:
            logger.warning("WebSocket token missing required userId claim")
            await websocket.send(json.dumps({
                "type": "error",
                "data": {"text": "Authentication failed: invalid token."}
            }))
            await websocket.close(code=1008, reason="Invalid authentication token")
            return

        # Eagerly warm caches for token-authenticated users (tracked-task pattern)
        _warm_user_caches(token_user_id)
    elif _INTERNAL_API_SECRET:
        logger.warning(f"WebSocket connection {client_id} has no valid token (INTERNAL_API_SECRET is set)")
        await websocket.send(json.dumps({
            "type": "error",
            "data": {"text": "Authentication failed: missing or invalid token."}
        }))
        await websocket.close(code=1008, reason="Missing or invalid authentication token")
        return

    weaviate_client = None
    deployment_listener_task = None
    current_user_id = token_user_id
    session_id = None       # Initialize to avoid UnboundLocalError in exception handler
    session_tasks = {}      # Track running workflow asyncio.Tasks per session

    
    try:
        postgres_client = PostgreSQLClient()
        weaviate_client = WeaviateClient(postgres_client)
        # Note: Agent will be created with websocket_sender in the message processing loop
        agent = None
        wf = None 
    except Exception as e: 
        # Log the error and send an error message to the client
        logger.error("Workflow Initialization Error: %s", e, exc_info=True)
        await websocket.send(json.dumps({
            "type": "error",
            "data": {
                "text": f"Unexpected error: {str(e)}",
                "session_id": session_id,
            }
        }))
        # Close the connection to weaviate if opened
        logger.debug(f"weaviate client is {weaviate_client}")
        if weaviate_client:
            weaviate_client.close()
        return

    # Send ready status to client
    await websocket.send(json.dumps({
        "type": "status",
        "data": {
            "status": "START",
        },
    }))

    # Listen for incoming messages
    try:
        # Main message loop. Will run each time a message is received from the frontend (aka sent by the user)
        async for message in websocket:
            # Rate limit check (per-user, Redis-backed; cross-transport)
            _rate_user = current_user_id
            if _rate_user and not chat_rate_is_allowed(_rate_user):
                logger.warning(f"Rate limit exceeded for user {_rate_user}")
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": "Rate limit exceeded. Please wait and try again."},
                }))
                continue

            logger.debug(f"Received message from client {client_id}: {message}")
            data = json.loads(message)

            # Handle connection initialization for the websocket
            if data.get('type') == 'init':
                current_user_id, deployment_listener_task = await handle_init(
                    data, websocket, current_user_id, deployment_listener_task
                )
                continue
            
            # Handle confirmation responses - WebSocket Connection Refresh Feature
            # When a user reconnects and sends a confirmation response, we update the workflow's
            # WebSocket connection to use the new connection instead of the old closed one.
            if data.get('type') == 'confirmation_response':
                logger.info(f"WEBSOCKET: Received confirmation response via connection {client_id}: {data.get('confirmation_id')}")

                # Extract user_id and session_id from confirmation response
                response_user_id = data.get('user_id')
                response_session_id = data.get('session_id')

                # On reconnect, refresh the Agent's WS sender so any in-flight
                # confirmation reply lands on the live socket. The active
                # sender ContextVar inside record_event is updated by the
                # workflow that owns the new connection; nothing to register
                # in cloud_tools anymore.
                if response_user_id and response_session_id:
                    if agent and hasattr(agent, 'update_websocket_sender'):
                        new_websocket_sender = create_websocket_sender(websocket, response_user_id, response_session_id)
                        agent.update_websocket_sender(new_websocket_sender, asyncio.get_event_loop())
                        logger.info(
                            "WEBSOCKET: Refreshed agent sender on reconnect (user=%s session=%s)",
                            response_user_id, response_session_id,
                        )

                await handle_websocket_confirmation_response(data)

                continue
            
            # Handle control messages (like cancel)
            if data.get('type') == 'control':
                if data.get('action') == 'cancel':
                    logger.debug(f"Cancel request received from client {client_id}")
                    # Extract identifiers carried by the control message (defensive – may be absent)
                    session_id = data.get('session_id')
                    user_id = data.get('user_id')

                    # Cancel any pending confirmations to unblock waiting threads
                    from utils.cloud.infrastructure_confirmation import cancel_pending_confirmations_for_session
                    cancelled_count = cancel_pending_confirmations_for_session(session_id)
                    if cancelled_count > 0:
                        logger.info(f"Cancelled {cancelled_count} pending confirmation(s) for session {session_id}")

                    # Retrieve the running task and its workflow for this session
                    running, cancel_wf = session_tasks.get(session_id, (None, None))

                    logger.debug(f"Attempting to cancel running workflow task for session {session_id}")

                    if running and not running.done():
                        # Ask the task to cancel **first** so no more chunks are appended
                        running.cancel()

                        try:
                            # Await the task so cancellation propagates cleanly
                            await running
                        except asyncio.CancelledError:
                            logger.info(f"Workflow task for session {session_id} acknowledged cancellation")

                        # Single-source-of-truth terminal write. The chat_events
                        # partial UNIQUE on (message_id, type) ensures this is a
                        # no-op (returns seq=0) if the workflow already finalized
                        # naturally — no duplicate messages, no competing saves.
                        # The workflow's own save_context_history / UI write at
                        # workflow.py:1205-1217 owns persistence; we only need to
                        # stamp the terminal event.
                        if cancel_wf and session_id and user_id:
                            try:
                                org_id_for_cancel = (
                                    getattr(cancel_wf, "_org_id", None)
                                    or get_org_id_for_user(user_id)
                                )
                                active_message_id = getattr(cancel_wf, "_active_message_id", None)
                                if not active_message_id:
                                    from chat.backend.agent.utils.persistence.chat_events import (
                                        get_active_stream_id,
                                    )
                                    active = await get_active_stream_id(
                                        session_id=session_id,
                                        org_id=org_id_for_cancel or "",
                                    )
                                    if active and ":" in active:
                                        _, active_message_id = active.split(":", 1)
                                if active_message_id and org_id_for_cancel:
                                    from chat.backend.agent.utils.persistence.chat_events import (
                                        record_event,
                                    )
                                    seq = await record_event(
                                        session_id=session_id,
                                        org_id=org_id_for_cancel,
                                        type="assistant_interrupted",
                                        payload={"reason": "user_cancelled"},
                                        message_id=active_message_id,
                                        agent_id="main",
                                    )
                                    logger.info(
                                        "[Cancel] assistant_interrupted recorded session=%s msg=%s seq=%s",
                                        session_id, active_message_id, seq,
                                    )
                                else:
                                    logger.info(
                                        "[Cancel] no active message to mark interrupted for session %s",
                                        session_id,
                                    )
                            except Exception as e:
                                logger.warning("[Cancel] failed to record assistant_interrupted: %s", e)
                
                # Send END status to frontend after cancellation cleanup
                try:
                    end_response = {
                        "type": "status",
                        "data": {"status": "END"},
                        "isComplete": True,
                        "session_id": session_id
                    }
                    await websocket.send(json.dumps(end_response))
                    logger.info(f"Sent END status to frontend after cancellation for session {session_id}")
                except Exception as e:
                    logger.warning(f"Failed to send END status after cancellation for session {session_id}: {e}")

                # Nothing more to do for this control message – return to listening loop
                continue
            
            # Extract the question from the incoming data
            question = data.get('query')
            
            logger.info(f"Processing question: {question}")
            
            user_id = data.get('user_id')  # Extract user_id from the incoming data
            session_id = data.get('session_id')  # Extract session_id from the incoming data

            # Server-side validation: token identity is authoritative when present.
            if current_user_id:
                if user_id and user_id != current_user_id:
                    logger.warning(
                        "Message rejected: token user %r does not match message user_id %r",
                        current_user_id,
                        user_id,
                    )
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {"text": "Authentication failed: user identity mismatch."}
                    }))
                    continue
                user_id = current_user_id
            elif user_id:
                if not validate_user_exists(user_id):
                    logger.warning(f"Message rejected: unverified user_id {user_id!r}")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {"text": "Authentication failed: invalid user identity."}
                    }))
                    continue
                current_user_id = user_id

            # Resolve org for tenant-scoped DB queries
            org_id = get_org_id_for_user(user_id) if user_id else None

            # RBAC: block viewers from sending messages in incident-linked sessions
            _rbac_incident_id = None
            if session_id and user_id and org_id:
                try:
                    from utils.db.connection_pool import db_pool
                    with db_pool.get_admin_connection() as conn:
                        with conn.cursor() as cur:
                            set_rls_context(cur, conn, user_id, log_prefix="[Chatbot:RBAC]")
                            cur.execute(
                                "SELECT incident_id FROM chat_sessions WHERE id = %s AND org_id = %s",
                                (session_id, org_id),
                            )
                            row = cur.fetchone()
                    if row and row[0]:
                        _rbac_incident_id = str(row[0])
                        from utils.auth.enforcer import get_enforcer
                        enforcer = get_enforcer()
                        if not enforcer.enforce(user_id, org_id, "incidents", "write"):
                            logger.warning(
                                "RBAC denied: viewer user=%s tried to chat in incident session=%s",
                                user_id, session_id,
                            )
                            await websocket.send(json.dumps({
                                "type": "error",
                                "data": {"text": "You do not have permission to interact with incident investigations."}
                            }))
                            continue
                except Exception as e:
                    logger.error("Error checking incident session RBAC: %s", e)
            
            # Get verified providers (cloud + SkillRegistry-validated integrations)
            from chat.background.rca_prompt_builder import get_user_providers
            if user_id:
                provider_preference = get_user_providers(user_id)
            else:
                provider_preference = None

            # Extract direct_tool_call which is sent at the top level of the message
            direct_tool_call = data.get('direct_tool_call')
            # Handle both old single provider format and new multiple provider format
            if isinstance(provider_preference, str):
                provider_preference = [provider_preference]
            if isinstance(provider_preference, list):
                # Build valid_providers list, conditionally including ovh
                from utils.flags.feature_flags import is_ovh_enabled
                valid_providers = ['gcp', 'azure', 'aws', 'scaleway', 'tailscale']
                if is_ovh_enabled():
                    valid_providers.append('ovh')
                provider_preference = [p for p in provider_preference if p in valid_providers]
                if not provider_preference:
                    provider_preference = None
            else:
                provider_preference = None
            selected_project_id = data.get('selected_project_id')  # Extract selected project ID if provided
            model = data.get('model')  # Extract selected model from frontend
            mode_input = data.get('mode')    # Extract chat mode (agent / ask)
            attachments = data.get('attachments', []) # Extract file attachments if present
            trigger_rca_requested = data.get('trigger_rca') is True

            mode = _normalize_mode(mode_input)

            # Extract UI state to save with the session
            ui_state = data.get('ui_state', {
                'selectedModel': model,
                'selectedMode': mode,
                'selectedProviders': provider_preference or []
            })
            if not user_id:
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": "Missing user_id in the message."}
                }))
                continue
            logger.info(f"Processing question from user {user_id}: {question}")
            logger.info(f"Using chat session: {session_id} and provider_preference: {provider_preference}")
            
            # Check if this is a direct tool call that should bypass the AI.
            # Both transports route through dispatch_direct_tool_call.
            if direct_tool_call:
                logger.info(f"Direct tool call requested: {direct_tool_call}")
                from chat.backend.agent.utils.direct_tool_dispatch import (
                    dispatch_direct_tool_call,
                )
                outcome = await dispatch_direct_tool_call(
                    payload=direct_tool_call,
                    user_id=user_id,
                    session_id=session_id,
                    mode=mode,
                    provider_preference=provider_preference,
                    selected_project_id=selected_project_id,
                )
                if outcome.status == "ok":
                    await websocket.send(json.dumps({
                        "type": "tool_result",
                        "data": {
                            "tool_name": outcome.tool_name,
                            "result": outcome.result,
                            "session_id": session_id,
                        },
                    }))
                    logger.info(f"Direct tool call completed: {outcome.tool_name}")
                else:
                    err_data = {
                        "text": outcome.error or "Direct tool dispatch failed",
                        "session_id": session_id,
                    }
                    if outcome.code:
                        err_data["code"] = outcome.code
                    await websocket.send(json.dumps({"type": "error", "data": err_data}))
                continue


            # Start deployment listener if user_id changed
            if current_user_id != user_id:
                # Cancel existing deployment listener
                if deployment_listener_task:
                    deployment_listener_task.cancel()
                    try:
                        await deployment_listener_task
                    except asyncio.CancelledError:
                        pass
                
                # Start new deployment listener for this user
                current_user_id = user_id
                # Deployment listener removed

            # Set the session variable for RLS enforcement (user + org)
            try:
                postgres_client.set_user_context(user_id, org_id=org_id)
            except Exception as e:
                logger.error("Failed to set user context: %s", e, exc_info=True)
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {"text": "Internal error setting user context.", "session_id": session_id}
                }))
                continue


            # EARLY VALIDATION: Check token count BEFORE creating any messages, states, or workflows
            # This prevents large messages from entering the system at all
            try:
                input_token_count = ChatContextManager.count_tokens_in_messages([HumanMessage(content=question)], "gpt-4")
                logger.info(f"User input token count: {input_token_count}")
                
                if input_token_count > 20000:
                    logger.warning(f"User input exceeds 20k token limit: {input_token_count} tokens")
                    await websocket.send(json.dumps({
                        "type": "error",
                        "data": {
                            "text": f"Your message is too long ({input_token_count} tokens). Please limit your message to 20,000 tokens (approximately 80,000 characters).",
                            "severity": "error",
                            "session_id": session_id,
                        }
                    }))
                    continue  # Skip processing this message
                    
            except Exception as e:
                logger.error(f"Error counting input tokens: {e}")
                # Block processing if token counting fails for safety
                await websocket.send(json.dumps({
                    "type": "error",
                    "data": {
                        "text": "Unable to process message due to token counting error. Please try again or contact support if this persists.",
                        "severity": "error",
                        "session_id": session_id,
                    }
                }))
                continue  # Skip processing this message when token counting fails

            # Create websocket sender function for tool calls
            websocket_sender = create_websocket_sender(websocket, user_id, session_id)
            
            # Set user context with session_id for terminal pod isolation
            set_user_context(
                user_id=user_id,
                session_id=session_id,
                provider_preference=provider_preference,
                selected_project_id=selected_project_id,
                mode=mode,
            )
            logger.info(f"Set user context with session_id {session_id} for terminal pod isolation")

            # Create agent and workflow with websocket sender if not already created
            if agent is None:
                agent = Agent(weaviate_client=weaviate_client, postgres_client=postgres_client, websocket_sender=websocket_sender, event_loop=asyncio.get_event_loop())
                logger.info(f"Created agent with websocket sender")
            
            # Create session-specific workflow instance to allow resumption while preventing cross-session leakage
            # Generate a temporary session_id if none provided (for new chats)
            effective_session_id = session_id or f"temp_{user_id}_{uuid.uuid4().hex[:8]}"
            wf = Workflow(agent, effective_session_id)
            logger.info(f"Created new workflow instance with session_id: {effective_session_id}")


            # Create the message content with attachments if present
            if attachments:
                # Create multimodal message content for OpenRouter
                content_parts = [{"type": "text", "text": question}]
                
                for attachment in attachments:
                    file_data = attachment.get('file_data', '')
                    file_type = attachment.get('file_type', '')
                    filename = attachment.get('filename', 'unknown')
                    
                    if file_type.startswith('image/'):
                        # Add image to content parts
                        # Frontend now sends only base64 data, so we need to reconstruct the data URL
                        data_url = f"data:{file_type};base64,{file_data}"
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": data_url
                            }
                        })
                        logger.info(f" Added image to multimodal content: {filename} ({len(file_data)} chars base64)")
                    elif file_type == 'application/pdf':
                        # Add PDF as file content
                        content_parts.append({
                            "type": "file",
                            "file": {
                                "filename": filename,
                                "file_data": file_data  # Already includes data:application/pdf;base64, prefix
                            }
                        })
                
                # For multimodal content, we need to use the content parts format
                human_message = HumanMessage(content=content_parts)
            else:
                # Regular text-only message
                human_message = HumanMessage(content=question)

            # Prepare messages list
            if trigger_rca_requested:
                rca_instruction = (
                    "[RCA INVESTIGATION REQUESTED]\n"
                    "The user has explicitly requested a Root Cause Analysis investigation. "
                    "You MUST call the trigger_rca tool with their message as the issue_description. "
                    "Extract a short title, affected service, and severity from their description.\n\n"
                )
                if isinstance(human_message.content, str):
                    human_message = HumanMessage(content=rca_instruction + human_message.content)
                elif isinstance(human_message.content, list):
                    human_message = HumanMessage(content=[{"type": "text", "text": rca_instruction}] + human_message.content)

            messages_list = [human_message]

            # Resolve incident_id — reuse result from RBAC check to avoid duplicate query
            _incident_id = _rbac_incident_id

            state = State(
                user_id=user_id,
                session_id=session_id,
                incident_id=_incident_id,
                org_id=org_id,
                provider_preference=provider_preference,
                selected_project_id=selected_project_id,
                messages=messages_list,
                question=question,
                attachments=attachments,
                model=model,
                mode=mode,
                trigger_rca_requested=bool(trigger_rca_requested),
            )

            logger.info(f"Created state with {len(attachments) if attachments else 0} attachments for regular query")
            logger.info(f"WebSocket sender initialized: {websocket_sender is not None}")

            # Launch workflow processing as async task without blocking
            # Set UI state in workflow before processing so it gets saved
            wf._ui_state = ui_state
            # Stamp identifiers the cancel handler reads to record assistant_interrupted.
            # `org_id` is the in-scope value from the init handshake; fall back to a
            # DB lookup if it wasn't supplied so cancel emits never silently no-op.
            wf._org_id = org_id or (get_org_id_for_user(user_id) if user_id else None)
            wf._active_message_id = None

            task = asyncio.create_task(process_workflow_async(wf, state, websocket, user_id))
            session_tasks[effective_session_id] = (task, wf)
            task.add_done_callback(lambda _t, _sid=effective_session_id: session_tasks.pop(_sid, None))

            continue  # Immediately return to listening for next message

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"WebSocket connection closed gracefully for client {client_id}")
    except Exception as e:
        logger.error("Error processing incoming message: %s", e, exc_info=True)
        try:
            await websocket.send(json.dumps({
                "type": "error",
                "data": {"text": f"Unexpected error: {str(e)}", "session_id": session_id},
            }))
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Could not send error message - connection already closed")
    finally:
        logger.info(f"Cleaning up connection for client {client_id}")
        
        # Note: Removed per-user workflow cleanup - users can now run multiple concurrent sessions
        
        # Cancel deployment listener task
        if deployment_listener_task:
            deployment_listener_task.cancel()
            try:
                await deployment_listener_task
            except asyncio.CancelledError:
                pass
        
        if weaviate_client:
            weaviate_client.close()

async def main():
    """Start the WebSocket server and health check endpoint."""
    WS_PORT = 5006
    HEALTH_PORT = 5007
    logger.info("Starting WebSocket server...")

    # Clean up old terraform files on startup
    cleanup_terraform_directory()

    # Start HTTP server (health check + internal API)
    http_server = await asyncio.start_server(handle_http_request, "0.0.0.0", HEALTH_PORT)
    logger.info(f"HTTP server (health + internal API) listening on port {HEALTH_PORT}")

    async with websockets.serve(
        handle_connection,
        host="0.0.0.0",  # Allow external access
        port=WS_PORT,
        ping_interval=20,  # Send ping every 20 seconds
        ping_timeout=10,   # Wait 10 seconds for pong response
        max_size=10 * 1024 * 1024,  # 10MB max message size
        compression=None   # Disable compression to avoid frame issues
    ):
        logger.info(f"WebSocket server listening on port {WS_PORT}")
        await asyncio.Future()

# Terraform cleanup is now handled by utils/terraform_cleanup.py

if __name__ == "__main__":
    try:
        # Start MCP preloader service for faster chat responses
        try:
            from chat.backend.agent.tools.mcp_preloader import start_mcp_preloader
            mcp_preloader = start_mcp_preloader()
            logger.info("MCP Preloader service started successfully")
        except Exception as e:
            logger.warning(f"Failed to start MCP preloader service: {e}")
        
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
