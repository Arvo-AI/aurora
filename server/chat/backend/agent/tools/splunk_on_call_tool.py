"""Read-only Splunk On-Call incident query tool."""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from routes.splunk_on_call.helpers import SplunkOnCallAPIError, SplunkOnCallClient
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)


class QuerySplunkOnCallArgs(BaseModel):
    phase: str = Field(
        default="all",
        description="Incident phase: all, unacked, acked, or resolved",
    )
    routing_key_contains: str = Field(
        default="",
        description="Optional case-insensitive routing-key substring",
    )
    limit: int = Field(default=50, ge=1, le=100)


def is_splunk_on_call_connected(user_id: str) -> bool:
    creds = get_token_data(user_id, "splunk_on_call") or {}
    return bool(creds.get("api_id") and creds.get("api_key"))


def _filter_incidents(
    incidents: list[dict],
    phase: str,
    routing_key_contains: str,
    limit: int,
) -> tuple[list[dict], bool]:
    normalized_phase = phase.strip().upper()
    if normalized_phase != "ALL":
        incidents = [
            item
            for item in incidents
            if str(item.get("currentPhase") or "").upper() == normalized_phase
        ]
    needle = routing_key_contains.strip().lower()
    if needle:
        incidents = [
            item
            for item in incidents
            if needle in str(item.get("routingKey") or "").lower()
        ]
    return incidents[:limit], len(incidents) > limit


def query_splunk_on_call(
    phase: str = "all",
    routing_key_contains: str = "",
    limit: int = 50,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return json.dumps({"error": "User context not available"})
    creds = get_token_data(user_id, "splunk_on_call") or {}
    if not creds.get("api_id") or not creds.get("api_key"):
        return json.dumps({"error": "Splunk On-Call is not connected"})

    client = SplunkOnCallClient(creds["api_id"], creds["api_key"])
    try:
        incidents = client.list_incidents()
    except SplunkOnCallAPIError:
        logger.exception("[SPLUNK_ON_CALL_TOOL] Incident query failed")
        return json.dumps({"error": "Unable to query Splunk On-Call"})

    results, truncated = _filter_incidents(
        incidents, phase, routing_key_contains, limit
    )
    return json.dumps({
        "success": True,
        "count": len(results),
        "results": results,
        "truncated": truncated,
    })
