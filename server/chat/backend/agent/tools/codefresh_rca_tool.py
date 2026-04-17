"""
Codefresh RCA Tool - Investigation tool for Root Cause Analysis.

Provides on-demand enrichment via Codefresh REST API:
- Build details and logs
- Pipeline configuration
- Project listing
Plus DB-only queries for stored deployment events and trace context.
"""

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .jenkins_rca_tool import (
    _action_recent_deployments,
    _action_trace_context,
)

logger = logging.getLogger(__name__)


class CodefreshRCAArgs(BaseModel):
    action: Literal[
        "recent_deployments",
        "build_detail",
        "pipeline_info",
        "build_logs",
        "list_builds",
        "trace_context",
    ] = Field(description="Investigation action to perform")
    build_id: Optional[str] = Field(default=None, description="Codefresh build ID to investigate")
    pipeline_id: Optional[str] = Field(default=None, description="Codefresh pipeline name/ID")
    service: Optional[str] = Field(default=None, description="Service name filter for recent_deployments")
    time_window_hours: Optional[int] = Field(default=24, description="Lookback window in hours for recent_deployments")
    deployment_event_id: Optional[int] = Field(default=None, description="Deployment event ID for trace_context lookup")


def _get_client_for_codefresh_user(user_id: str):
    """Build a CodefreshClient from the user's stored credentials."""
    from utils.auth.token_management import get_token_data
    from connectors.codefresh_connector.api_client import CodefreshClient

    creds = get_token_data(user_id, "codefresh")
    if not creds:
        logger.warning("[CODEFRESH_RCA] No stored credentials for user %s", user_id)
        return None
    base_url = creds.get("base_url")
    api_token = creds.get("api_token")
    if not base_url or not api_token:
        logger.warning("[CODEFRESH_RCA] Incomplete credentials for user %s", user_id)
        return None
    return CodefreshClient(base_url=base_url, api_token=api_token)


def codefresh_rca(
    action: str,
    build_id: Optional[str] = None,
    pipeline_id: Optional[str] = None,
    service: Optional[str] = None,
    time_window_hours: int = 24,
    deployment_event_id: Optional[int] = None,
    **kwargs,
) -> str:
    """Unified Codefresh investigation tool for RCA."""
    user_id = kwargs.get("user_id", "")

    if not user_id:
        return json.dumps({"error": "No user context. Run this from an authenticated session."})

    if action == "recent_deployments":
        return _action_recent_deployments(user_id, service, time_window_hours, provider="codefresh")
    elif action == "trace_context":
        return _action_trace_context(user_id, deployment_event_id)

    client = _get_client_for_codefresh_user(user_id)
    if not client:
        return json.dumps({"error": "Codefresh is not connected. Configure credentials in Settings > Connectors > Codefresh."})

    if action == "build_detail":
        return _action_build_detail(client, build_id)
    elif action == "pipeline_info":
        return _action_pipeline_info(client, pipeline_id)
    elif action == "build_logs":
        return _action_build_logs(client, build_id)
    elif action == "list_builds":
        return _action_list_builds(client, pipeline_id)
    else:
        return json.dumps({"error": f"Unknown action: {action}"})


# ------------------------------------------------------------------
# Action implementations
# ------------------------------------------------------------------

def _action_build_detail(client, build_id: Optional[str]) -> str:
    """Fetch full build details from Codefresh API."""
    if not build_id:
        return json.dumps({"error": "build_id is required"})
    success, data, error = client.get_build(build_id)
    if not success:
        return json.dumps({"error": error or "Failed to fetch build detail"})
    return json.dumps(data, default=str)


def _action_pipeline_info(client, pipeline_id: Optional[str]) -> str:
    """Fetch pipeline configuration and details."""
    if not pipeline_id:
        return json.dumps({"error": "pipeline_id is required"})
    success, data, error = client.get_pipeline(pipeline_id)
    if not success:
        return json.dumps({"error": error or "Failed to fetch pipeline info"})
    return json.dumps(data, default=str)


def _action_build_logs(client, build_id: Optional[str]) -> str:
    """Fetch build log output from Codefresh API."""
    if not build_id:
        return json.dumps({"error": "build_id is required"})
    success, data, error = client.get_build_logs(build_id)
    if not success:
        return json.dumps({"error": error or "Failed to fetch build logs"})
    if isinstance(data, str):
        return json.dumps({"logs": data})
    return json.dumps(data, default=str)


def _action_list_builds(client, pipeline_id: Optional[str]) -> str:
    """List recent builds, optionally filtered by pipeline."""
    success, data, error = client.list_builds(pipeline_id=pipeline_id, limit=20)
    if not success:
        return json.dumps({"error": error or "Failed to list builds"})
    return json.dumps(data, default=str)
