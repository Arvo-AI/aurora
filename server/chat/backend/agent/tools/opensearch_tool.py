"""OpenSearch log search tool for RCA agent."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

MAX_HITS = 200


def is_opensearch_connected(user_id: str) -> bool:
    """Return True if the user has OpenSearch credentials stored."""
    try:
        creds = get_token_data(user_id, "opensearch")
        return bool(creds and creds.get("endpoint") and creds.get("username") and creds.get("password"))
    except Exception:
        return False


def _get_client(user_id: str):
    """Build an OpenSearchClient from stored credentials."""
    from connectors.opensearch_connector.client import OpenSearchClient

    creds = get_token_data(user_id, "opensearch")
    if not creds:
        raise RuntimeError("OpenSearch credentials not found. Please connect OpenSearch first.")
    return OpenSearchClient(
        endpoint=creds["endpoint"],
        username=creds["username"],
        password=creds["password"],
        index_pattern=creds.get("index_pattern", "*"),
        verify_ssl=creds.get("verify_ssl", True),
        max_retries=creds.get("max_retries", 2),
    )


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------

class OpenSearchSearchArgs(BaseModel):
    query: str = Field(description="Lucene query string, e.g. 'error AND service:api'")
    index: Optional[str] = Field(default=None, description="Index or pattern to search, e.g. 'logs-*'. Defaults to the configured index pattern.")
    start_time: Optional[str] = Field(default="now-1h", description="Start time — relative ('now-1h', 'now-30m') or ISO-8601")
    end_time: Optional[str] = Field(default=None, description="End time — relative or ISO-8601. Defaults to now.")
    size: int = Field(default=50, ge=1, le=MAX_HITS, description="Max number of log entries to return (1–200)")
    timestamp_field: str = Field(default="@timestamp", description="Name of the timestamp field in your index")


class OpenSearchListIndicesArgs(BaseModel):
    pattern: Optional[str] = Field(default=None, description="Index pattern filter, e.g. 'logs-*'. Defaults to all indices.")


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def search_opensearch(
    query: str,
    index: Optional[str] = None,
    start_time: Optional[str] = "now-1h",
    end_time: Optional[str] = None,
    size: int = 50,
    timestamp_field: str = "@timestamp",
    user_id: Optional[str] = None,
) -> str:
    """Search OpenSearch logs using a Lucene query."""
    if not user_id:
        return "Error: user_id not provided"

    try:
        client = _get_client(user_id)
        result = client.search(
            query=query,
            index=index,
            start_time=start_time,
            end_time=end_time,
            size=min(size, MAX_HITS),
            timestamp_field=timestamp_field,
        )

        total = result.get("total", 0)
        hits = result.get("hits", [])

        if not hits:
            return f"No results found for query: {query!r} in index {result.get('index', '*')}"

        lines = [f"OpenSearch results — index: {result['index']} | query: {query!r} | total hits: {total} | showing: {len(hits)}"]
        for i, doc in enumerate(hits, 1):
            # Extract common log fields
            ts = doc.get("@timestamp") or doc.get("timestamp") or ""
            level = doc.get("level") or doc.get("log.level") or doc.get("severity") or ""
            msg = doc.get("message") or doc.get("msg") or doc.get("log") or ""
            svc = doc.get("service") or doc.get("service.name") or doc.get("kubernetes.labels.app") or ""

            summary_parts = []
            if ts:
                summary_parts.append(f"[{ts}]")
            if level:
                summary_parts.append(f"[{level.upper()}]")
            if svc:
                summary_parts.append(f"[{svc}]")
            if msg:
                summary_parts.append(msg[:300])

            if summary_parts:
                lines.append(f"{i}. {' '.join(summary_parts)}")
            else:
                import json as _json
                lines.append(f"{i}. {_json.dumps(doc)[:400]}")

        if total > len(hits):
            lines.append(f"\n... {total - len(hits)} more results not shown. Use a more specific query or smaller time range.")

        return "\n".join(lines)

    except Exception:
        logger.warning("[OPENSEARCH TOOL] search_opensearch failed for user %s: connection error", user_id)
        return "OpenSearch search failed: connection error"


def list_opensearch_indices(
    pattern: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """List available OpenSearch indices."""
    if not user_id:
        return "Error: user_id not provided"

    try:
        client = _get_client(user_id)
        indices = client.list_indices(pattern=pattern)

        if not indices:
            return "No indices found matching pattern: " + (pattern or "*")

        lines = [f"OpenSearch indices ({len(indices)} found):"]
        for idx in indices[:100]:
            name = idx.get("index", "?")
            health = idx.get("health", "?")
            docs = idx.get("docs.count", "?")
            size = idx.get("store.size", "?")
            lines.append(f"  - {name} | health={health} | docs={docs} | size={size}")

        if len(indices) > 100:
            lines.append(f"  ... and {len(indices) - 100} more")

        return "\n".join(lines)

    except Exception:
        logger.warning("[OPENSEARCH TOOL] list_opensearch_indices failed for user %s: connection error", user_id)
        return "Failed to list OpenSearch indices: connection error"
