"""Splunk On-Call (VictorOps) query tools for the RCA agent."""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 32_000


class GetVictorOpsIncidentsArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=100, description="Maximum number of recent incidents to return (1–100)")


class GetVictorOpsTeamsArgs(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def is_victorops_connected(user_id: str) -> bool:
    """Check if a user has valid Splunk On-Call credentials stored."""
    try:
        from utils.auth.token_management import get_token_data

        creds = get_token_data(user_id, "victorops")
        return bool(creds and creds.get("api_id") and creds.get("api_key"))
    except Exception:
        logger.debug("VictorOps connection check failed for user %s", user_id)
        return False


def _get_client(user_id: str):
    from utils.auth.token_management import get_token_data
    from routes.victorops.victorops_helpers import VictorOpsClient

    creds = get_token_data(user_id, "victorops")
    if not creds:
        return None
    return VictorOpsClient(api_id=creds["api_id"], api_key=creds["api_key"])


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def get_victorops_incidents(
    limit: int = 20,
    user_id: Optional[str] = None,
) -> str:
    """Retrieve recent incidents from Splunk On-Call."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _get_client(user_id)
    if not client:
        return json.dumps({"error": "Splunk On-Call not connected. Please connect it first."})

    try:
        limit = max(1, min(limit, 100))
        data = client.get_incidents(limit=limit)
        incidents = data.get("incidents", [])[:limit]

        # Trim iteratively to stay within the agent response budget
        while len(json.dumps(incidents)) > MAX_OUTPUT_SIZE and len(incidents) > 1:
            incidents = incidents[: len(incidents) // 2]

        return json.dumps({
            "success": True,
            "count": len(incidents),
            "results": incidents,
        })
    except Exception:
        logger.warning("[VICTOROPS-TOOL] get_incidents failed for user=%s: API error", user_id)
        return json.dumps({"error": "Error fetching incidents: API error"})


def get_victorops_teams(
    user_id: Optional[str] = None,
) -> str:
    """Retrieve teams and on-call information from Splunk On-Call."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _get_client(user_id)
    if not client:
        return json.dumps({"error": "Splunk On-Call not connected. Please connect it first."})

    try:
        data = client.get_teams()
        teams = data.get("teams", [])
        return json.dumps({
            "success": True,
            "count": len(teams),
            "results": teams,
        })
    except Exception:
        logger.warning("[VICTOROPS-TOOL] get_teams failed for user=%s: API error", user_id)
        return json.dumps({"error": "Error fetching teams: API error"})
