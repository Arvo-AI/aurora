"""Cross-agent fingerprint dedup backed by Redis."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_RESULT_PREVIEW_CAP = 4096


class ToolDedupHit(Exception):
    def __init__(self, result_preview: str, from_agent: Optional[str] = None, tool_name: Optional[str] = None):
        super().__init__(result_preview)
        self.result_preview = result_preview
        self.from_agent = from_agent
        self.tool_name = tool_name


def compute_fingerprint(tool_name: str, args: dict) -> str:
    # default=str keeps non-JSON-serializable arg values from crashing canonicalisation.
    canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    payload = f"{tool_name}|{canonical}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _redis_key(incident_id: str, fingerprint: str) -> str:
    return f"rca:{incident_id}:fingerprints:{fingerprint}"


def _check_sync(incident_id: str, fingerprint: str) -> Optional[dict]:
    try:
        client = get_redis_client()
        if client is None:
            return None
        raw = client.get(_redis_key(incident_id, fingerprint))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as e:
            logger.warning(f"dedup.check_fingerprint decode failed: {e}")
            return None
    except Exception as e:
        logger.warning(f"dedup.check_fingerprint failed: {e}")
        return None


def _store_sync(incident_id: str, fingerprint: str, value: dict, ttl: int) -> None:
    try:
        client = get_redis_client()
        if client is None:
            return
        client.set(_redis_key(incident_id, fingerprint), json.dumps(value), ex=ttl)
    except Exception as e:
        logger.warning(f"dedup.store_fingerprint failed: {e}")


async def check_fingerprint(incident_id: str, fingerprint: str) -> Optional[dict]:
    if not incident_id or not fingerprint:
        return None
    try:
        return await asyncio.to_thread(_check_sync, incident_id, fingerprint)
    except Exception as e:
        logger.warning(f"dedup.check_fingerprint async failed: {e}")
        return None


async def store_fingerprint(
    incident_id: str,
    fingerprint: str,
    result: dict,
    ttl: int = 3600,
) -> None:
    if not incident_id or not fingerprint:
        return
    try:
        preview = (result or {}).get("result_preview", "")
        if isinstance(preview, str) and len(preview) > _RESULT_PREVIEW_CAP:
            preview = preview[:_RESULT_PREVIEW_CAP]
        value = {
            "tool_name": (result or {}).get("tool_name"),
            "agent_id": (result or {}).get("agent_id"),
            "result_preview": preview,
            "stored_at": (result or {}).get("stored_at") or datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(_store_sync, incident_id, fingerprint, value, ttl)
    except Exception as e:
        logger.warning(f"dedup.store_fingerprint async failed: {e}")
