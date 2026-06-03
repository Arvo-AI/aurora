"""
Fly.io agent tool -- connection check and metrics query.

The primary agent interaction with Fly.io is through cloud_exec(provider='flyio', command='fly ...')
which uses the flyctl CLI. This module provides:
1. is_flyio_connected() -- connection gate for tool registration
2. A Prometheus metrics query tool for structured metrics access
"""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field
from utils.secrets.secret_ref_utils import get_user_token_data
from connectors.flyio_connector.api_client import FlyioClient

logger = logging.getLogger(__name__)


def is_flyio_connected(user_id: str) -> bool:
    """Check if Fly.io is connected for a user."""
    try:
        token_data = get_user_token_data(user_id, "flyio")
        return bool(token_data and "api_token" in token_data)
    except Exception:
        return False


class FlyioMetricsQueryArgs(BaseModel):
    """Arguments for querying Fly.io Prometheus metrics."""
    query: str = Field(description="PromQL query (e.g. 'fly_instance_up{app=\"myapp\"}', 'rate(fly_instance_cpu{app=\"myapp\"}[5m])')")
    time: Optional[str] = Field(default=None, description="Evaluation time (RFC3339 or Unix timestamp). Defaults to now.")


def query_flyio_metrics(query: str, time: Optional[str] = None, user_id: str = None, **kwargs) -> str:
    """Query Fly.io Prometheus metrics endpoint."""
    token_data = get_user_token_data(user_id, "flyio")
    if not token_data:
        return json.dumps({"error": "Fly.io not connected. Please connect your Fly.io account first."})

    api_token = token_data.get("api_token")
    org_slug = token_data.get("org_slug")

    if not api_token or not org_slug:
        return json.dumps({"error": "Incomplete Fly.io credentials"})

    try:
        client = FlyioClient(api_token, org_slug)
        result = client.query_prometheus(query, time_param=time)
    except Exception:
        logger.exception("Fly.io Prometheus query failed for: %s", query)
        return json.dumps({"error": "Prometheus query failed", "query": query})

    if result is None:
        return json.dumps({"error": f"Prometheus query failed for: {query}"})

    data = result.get("data", {})
    results = data.get("result", [])

    if not results:
        return json.dumps({"query": query, "results": [], "message": "No data returned"})

    formatted = []
    for r in results[:50]:
        metric = r.get("metric", {})
        value = r.get("value", [])
        formatted.append({
            "labels": metric,
            "value": value[1] if len(value) > 1 else None,
            "timestamp": value[0] if value else None,
        })

    response = {
        "query": query,
        "result_count": len(results),
        "results": formatted,
    }

    if len(results) > 50:
        response["truncated"] = True
        response["message"] = f"Showing 50 of {len(results)} results. Use more specific label selectors to narrow down."

    return json.dumps(response, indent=2)
