"""Splunk On-Call (VictorOps) query tools for the RCA agent."""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 32_000


class GetVictorOpsIncidentsArgs(BaseModel):
    limit: int = Field(default=20, description="Maximum number of recent incidents to return")


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
    **kwargs,
) -> str:
    """Retrieve recent incidents from Splunk On-Call."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _get_client(user_id)
    if not client:
        return json.dumps({"error": "Splunk On-Call not connected. Please connect it first."})

    try:
        from routes.victorops.victorops_helpers import VictorOpsAPIError

        data = client.get_incidents(limit=min(limit, 100))
        incidents = data.get("incidents", [])[:limit]

        # Trim to size
        results_str = json.dumps(incidents)
        if len(results_str) > MAX_OUTPUT_SIZE:
            incidents = incidents[: max(1, limit // 2)]

        return json.dumps({
            "success": True,
            "count": len(incidents),
            "results": incidents,
        })
    except Exception as exc:
        logger.exception("[VICTOROPS-TOOL] get_incidents failed for user=%s", user_id)
        return json.dumps({"error": f"Error fetching incidents: {str(exc)[:200]}"})


def get_victorops_teams(
    user_id: Optional[str] = None,
    **kwargs,
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
    except Exception as exc:
        logger.exception("[VICTOROPS-TOOL] get_teams failed for user=%s", user_id)
        return json.dumps({"error": f"Error fetching teams: {str(exc)[:200]}"})
