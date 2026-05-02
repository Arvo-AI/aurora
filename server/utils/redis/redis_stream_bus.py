"""Redis Streams + pub/sub transport for chat SSE.

Per-message stream key:  chat:stream:{session_id}:{message_id}
Per-session wake channel: chat:wake:{session_id}

Each event entry has fields:
  seq:               str(int)   — chat_events.seq
  type:              str
  payload:           json-string
  agent_id:          str|""
  parent_agent_id:   str|""
  message_id:        str|""

Streams are capped at MAXLEN ~10000 with EXPIRE 3600 (1h after last write).
The wake channel is a lightweight pub/sub used to break the SSE generator out
of its blocking xread loop the moment a new event arrives.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import redis.asyncio as redis_async
    from redis.asyncio.client import PubSub

logger = logging.getLogger(__name__)


def _import_redis_async():
    """Lazy import — test envs stub `redis` as a flat MagicMock module so
    `redis.asyncio` can't be imported at module-load time. Defer to call time."""
    import redis.asyncio as ra
    return ra

_STREAM_MAXLEN = 10_000
_STREAM_TTL_SECONDS = 3600


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://redis:6379/0")


async def get_async_redis() -> Optional[Any]:
    """Build a fresh async Redis client bound to the current event loop.

    redis.asyncio clients hold a reference to the loop they were constructed on,
    so caching globally across short-lived per-request loops causes
    "got Future attached to a different loop" errors. Aurora opens a new loop
    per SSE request and per record_event call from background threads — both
    are short-lived enough that the connect overhead is negligible.

    Returns None if Redis is unreachable.
    """
    try:
        from utils.cache.redis_client import get_redis_ssl_kwargs
        ra = _import_redis_async()
        client = ra.from_url(
            _redis_url(),
            decode_responses=True,
            **get_redis_ssl_kwargs(),
        )
        await client.ping()
        return client
    except Exception as e:
        logger.warning("[redis_stream_bus] connect failed: %s", e)
        return None


def stream_key(session_id: str, message_id: str) -> str:
    return f"chat:stream:{session_id}:{message_id}"


def wake_channel(session_id: str) -> str:
    return f"chat:wake:{session_id}"


def cancel_channel(session_id: str) -> str:
    """Pub/sub channel used to cooperatively cancel an in-flight workflow run.

    A POST /api/chat/cancel publishes a single message here; long-running
    workflows (multi-agent + WS) subscribe before they start streaming and set
    an ``asyncio.Event`` on receipt, which their orchestrator/sub-agent nodes
    poll between steps to abort early.
    """
    return f"chat:cancel:{session_id}"


async def _close(client: Optional[Any]) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


async def xadd_event(
    *,
    session_id: str,
    message_id: Optional[str],
    type_: str,
    payload: dict,
    seq: int,
    agent_id: Optional[str] = None,
    parent_agent_id: Optional[str] = None,
) -> Optional[str]:
    """Append an event to the per-message Redis Stream and refresh its TTL.

    Returns the Redis stream entry ID, or None on failure (failure is logged
    at WARN — callers must not surface this to the request).
    """
    if not session_id or not message_id:
        return None
    client = await get_async_redis()
    if client is None:
        return None
    key = stream_key(session_id, message_id)
    fields = {
        "seq": str(seq),
        "type": type_,
        "payload": json.dumps(payload, default=str),
        "agent_id": agent_id or "",
        "parent_agent_id": parent_agent_id or "",
        "message_id": message_id or "",
    }
    try:
        entry_id = await client.xadd(
            key,
            fields,
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        await client.expire(key, _STREAM_TTL_SECONDS)
        return entry_id
    except Exception as e:
        logger.warning("[redis_stream_bus] xadd failed (key=%s): %s", key, e)
        return None
    finally:
        await _close(client)


async def publish_wake(session_id: str) -> None:
    """Wake any SSE listener tailing this session. Non-fatal on failure."""
    if not session_id:
        return
    client = await get_async_redis()
    if client is None:
        return
    try:
        await client.publish(wake_channel(session_id), "1")
    except Exception as e:
        logger.warning("[redis_stream_bus] publish failed (session=%s): %s", session_id, e)
    finally:
        await _close(client)


async def read_stream_replay(
    *,
    session_id: str,
    message_id: str,
    from_id: str = "0",
) -> list[dict]:
    """Read every entry in a stream from `from_id` onwards.

    Used when the SSE client reconnects with a Last-Event-ID and the active
    Redis Stream is the source of truth (e.g. events still in flight that
    haven't yet been queried back from Postgres).
    """
    client = await get_async_redis()
    if client is None:
        return []
    key = stream_key(session_id, message_id)
    try:
        raw = await client.xrange(key, min=from_id, max="+")
    except Exception as e:
        logger.warning("[redis_stream_bus] xrange failed (key=%s): %s", key, e)
        return []
    finally:
        await _close(client)
    return [_decode_entry(entry_id, fields) for entry_id, fields in raw]


async def tail_stream(
    *,
    session_id: str,
    message_id: str,
    last_id: str = "$",
    block_ms: int = 30_000,
) -> AsyncIterator[dict]:
    """Block-read new entries from a stream. Yields decoded entries as they arrive.

    `last_id="$"` starts from the next-arriving entry.
    Returns when the block times out without entries — caller decides whether
    to re-enter (heartbeat + retry).
    """
    client = await get_async_redis()
    if client is None:
        return
    key = stream_key(session_id, message_id)
    cursor = last_id
    try:
        while True:
            try:
                resp = await client.xread({key: cursor}, count=100, block=block_ms)
            except Exception as e:
                logger.warning("[redis_stream_bus] xread failed (key=%s): %s", key, e)
                return
            if not resp:
                return
            for _stream_key, entries in resp:
                for entry_id, fields in entries:
                    cursor = entry_id
                    yield _decode_entry(entry_id, fields)
    finally:
        await _close(client)


async def subscribe_wake(session_id: str) -> Optional[tuple[Any, Any]]:
    """Subscribe to the per-session wake channel. Returns (pubsub, client) so
    the caller can close BOTH after use; closing only the pubsub leaks the
    underlying connection pool because get_async_redis() builds a fresh client
    per call.
    """
    client = await get_async_redis()
    if client is None:
        return None
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(wake_channel(session_id))
        return pubsub, client
    except Exception as e:
        logger.warning("[redis_stream_bus] subscribe failed (session=%s): %s", session_id, e)
        await _close(client)
        return None


async def subscribe_cancel(session_id: str) -> Optional[tuple[Any, Any]]:
    """Subscribe to the per-session cancel channel. Returns (pubsub, client) so
    the caller can close BOTH after use.
    """
    client = await get_async_redis()
    if client is None:
        return None
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(cancel_channel(session_id))
        return pubsub, client
    except Exception as e:
        logger.warning(
            "[redis_stream_bus] subscribe_cancel failed (session=%s): %s",
            session_id, e,
        )
        await _close(client)
        return None


def _decode_entry(entry_id: str, fields: dict[str, Any]) -> dict:
    """Decode a Redis Stream entry into the SSE wire shape."""
    payload_raw = fields.get("payload") or "{}"
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        payload = {"_raw": payload_raw}
    seq_raw = fields.get("seq") or "0"
    try:
        seq = int(seq_raw)
    except ValueError:
        seq = 0
    return {
        "entry_id": entry_id,
        "seq": seq,
        "type": fields.get("type") or "",
        "payload": payload,
        "agent_id": fields.get("agent_id") or None,
        "parent_agent_id": fields.get("parent_agent_id") or None,
        "message_id": fields.get("message_id") or None,
    }
