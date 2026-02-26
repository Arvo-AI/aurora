"""ThousandEyes network intelligence tools for the RCA agent.

Provides LLM-callable tools that query a user's ThousandEyes account for
tests, test results, alerts, agents, and Internet Insights outage data.
Credentials are loaded from Vault via ``get_token_data(user_id, "thousandeyes")``.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.thousandeyes_connector.client import (
    ThousandEyesAPIError,
    ThousandEyesClient,
    get_thousandeyes_client,
)
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 120000
MAX_LIST_ITEMS = 100

NOT_CONNECTED_MSG = "ThousandEyes not connected. Ask the user to connect ThousandEyes first."


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _get_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "thousandeyes")
        if creds and creds.get("api_token"):
            return creds
        return None
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] Failed to get credentials: %s", exc)
        return None


def _build_client(user_id: str) -> Optional[ThousandEyesClient]:
    creds = _get_credentials(user_id)
    if not creds:
        return None
    try:
        return get_thousandeyes_client(
            user_id,
            api_token=creds["api_token"],
            account_group_id=creds.get("account_group_id"),
        )
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] Failed to build client: %s", exc)
        return None


def is_thousandeyes_connected(user_id: str) -> bool:
    return _get_credentials(user_id) is not None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return json.dumps({
        "_truncated": True,
        "_message": (
            "Response exceeded size limit and was cut off. "
            "Use filters to narrow the query."
        ),
        "partial_data": text[:limit],
    })


def _safe_json(obj: Any) -> str:
    try:
        return _truncate(json.dumps(obj, indent=2, default=str))
    except (TypeError, ValueError):
        return _truncate(str(obj))


def _truncate_list(
    items: List[Any],
    total: int,
    max_items: int = MAX_LIST_ITEMS,
    hint: str = "Use filters to narrow results.",
) -> tuple:
    if len(items) <= max_items:
        return items, {}
    return items[:max_items], {
        "_truncated": True,
        "_returned": max_items,
        "_total": total,
        "_message": f"Showing {max_items} of {total} results. {hint}",
    }


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------

class ThousandEyesListTestsArgs(BaseModel):
    test_type: Optional[str] = Field(
        default=None,
        description=(
            "Filter by test type: 'agent-to-server', 'agent-to-agent', 'bgp', "
            "'dns-server', 'dns-trace', 'dnssec', 'http-server', 'page-load', "
            "'web-transactions', 'api', 'sip-server', 'voice'. "
            "Leave empty to list all tests."
        ),
    )


class ThousandEyesGetTestResultsArgs(BaseModel):
    test_id: str = Field(description="The ThousandEyes test ID to get results for.")
    result_type: str = Field(
        default="network",
        description=(
            "Type of results to fetch: 'network' (latency, loss, jitter), "
            "'http' (response time, availability), 'path-vis' (hop-by-hop trace), "
            "'dns' (DNS resolution), 'bgp' (BGP routes)."
        ),
    )
    window: Optional[str] = Field(
        default=None,
        description="Time window for results (e.g. '1h', '6h', '12h', '1d'). Defaults to the latest round.",
    )


class ThousandEyesGetAlertsArgs(BaseModel):
    state: Optional[str] = Field(
        default=None,
        description="Filter by alert state: 'active' or 'cleared'. Leave empty for all.",
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by alert severity: 'major', 'minor', 'info'. Leave empty for all.",
    )
    window: Optional[str] = Field(
        default=None,
        description="Time window (e.g. '1h', '6h', '1d'). Defaults to active alerts.",
    )


class ThousandEyesGetAgentsArgs(BaseModel):
    agent_type: Optional[str] = Field(
        default=None,
        description="Filter by agent type: 'cloud' or 'enterprise'. Leave empty for all.",
    )


class ThousandEyesGetInternetInsightsArgs(BaseModel):
    outage_type: str = Field(
        default="network",
        description="Type of outage: 'network' (ISP/transit) or 'application' (SaaS/CDN).",
    )
    window: Optional[str] = Field(
        default=None,
        description="Time window (e.g. '1h', '6h', '1d'). Defaults to recent outages.",
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def thousandeyes_list_tests(
    *,
    user_id: Optional[str] = None,
    test_type: Optional[str] = None,
) -> str:
    """List all configured ThousandEyes tests."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _build_client(user_id)
    if not client:
        return json.dumps({"error": NOT_CONNECTED_MSG})

    try:
        tests = client.get_tests(test_type=test_type)
    except ThousandEyesAPIError as exc:
        return json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] list_tests failed: %s", exc)
        return json.dumps({"error": f"Unexpected error: {exc}"})

    if not tests:
        filter_desc = f" (type={test_type})" if test_type else ""
        return json.dumps({"tests": [], "message": f"No tests found{filter_desc}."})

    _TEST_FIELDS = ("testId", "testName", "type", "enabled", "interval", "server", "url", "createdDate")
    summaries = [{k: t.get(k) for k in _TEST_FIELDS} for t in tests]

    items, trunc_meta = _truncate_list(
        summaries, len(summaries),
        hint="Use test_type filter to narrow results.",
    )

    result: Dict[str, Any] = {
        "total_tests": len(summaries),
        "tests": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)


def thousandeyes_get_test_results(
    *,
    user_id: Optional[str] = None,
    test_id: str = "",
    result_type: str = "network",
    window: Optional[str] = None,
) -> str:
    """Get results for a specific ThousandEyes test."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    if not test_id:
        return json.dumps({"error": "test_id is required"})

    client = _build_client(user_id)
    if not client:
        return json.dumps({"error": NOT_CONNECTED_MSG})

    try:
        data = client.get_test_results(test_id, result_type=result_type, window=window)
    except ThousandEyesAPIError as exc:
        return json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] get_test_results failed: %s", exc)
        return json.dumps({"error": f"Unexpected error: {exc}"})

    if not data:
        return json.dumps({
            "test_id": test_id,
            "result_type": result_type,
            "message": "No results found for this test.",
        })

    return _safe_json(data)


def thousandeyes_get_alerts(
    *,
    user_id: Optional[str] = None,
    state: Optional[str] = None,
    severity: Optional[str] = None,
    window: Optional[str] = None,
) -> str:
    """Get ThousandEyes alerts."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _build_client(user_id)
    if not client:
        return json.dumps({"error": NOT_CONNECTED_MSG})

    try:
        alerts = client.get_alerts(state=state, severity=severity, window=window)
    except ThousandEyesAPIError as exc:
        return json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] get_alerts failed: %s", exc)
        return json.dumps({"error": f"Unexpected error: {exc}"})

    if not alerts:
        filter_parts = []
        if state:
            filter_parts.append(f"state={state}")
        if severity:
            filter_parts.append(f"severity={severity}")
        filter_desc = f" ({', '.join(filter_parts)})" if filter_parts else ""
        return json.dumps({"alerts": [], "message": f"No alerts found{filter_desc}."})

    summaries = []
    for a in alerts:
        summaries.append({
            "alertId": a.get("alertId"),
            "testId": a.get("testId"),
            "testName": a.get("testName"),
            "alertType": a.get("alertType"),
            "alertState": a.get("alertState"),
            "alertSeverity": a.get("alertSeverity"),
            "startDate": a.get("startDate"),
            "endDate": a.get("endDate"),
            "violationCount": a.get("violationCount"),
            "ruleExpression": a.get("ruleExpression"),
            "agents": [
                {"agentName": agent.get("agentName"), "active": agent.get("active")}
                for agent in (a.get("agents") or [])[:5]
            ],
        })

    items, trunc_meta = _truncate_list(
        summaries, len(summaries),
        hint="Use state/severity filters to narrow results.",
    )

    active_count = sum(1 for a in summaries if a.get("alertState") == "active")

    result: Dict[str, Any] = {
        "total_alerts": len(summaries),
        "active_alerts": active_count,
        "alerts": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)


def thousandeyes_get_agents(
    *,
    user_id: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> str:
    """List ThousandEyes agents and their status."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _build_client(user_id)
    if not client:
        return json.dumps({"error": NOT_CONNECTED_MSG})

    try:
        agents = client.get_agents(agent_type=agent_type)
    except ThousandEyesAPIError as exc:
        return json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] get_agents failed: %s", exc)
        return json.dumps({"error": f"Unexpected error: {exc}"})

    if not agents:
        filter_desc = f" (type={agent_type})" if agent_type else ""
        return json.dumps({"agents": [], "message": f"No agents found{filter_desc}."})

    _AGENT_FIELDS = ("agentId", "agentName", "agentType", "countryId", "location", "enabled", "agentState", "ipAddresses", "lastSeen")
    summaries = [{k: a.get(k) for k in _AGENT_FIELDS} for a in agents]

    items, trunc_meta = _truncate_list(
        summaries, len(summaries),
        hint="Use agent_type filter ('cloud' or 'enterprise') to narrow results.",
    )

    result: Dict[str, Any] = {
        "total_agents": len(summaries),
        "agents": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)


def thousandeyes_get_internet_insights(
    *,
    user_id: Optional[str] = None,
    outage_type: str = "network",
    window: Optional[str] = None,
) -> str:
    """Get Internet Insights outage data from ThousandEyes."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client = _build_client(user_id)
    if not client:
        return json.dumps({"error": NOT_CONNECTED_MSG})

    try:
        outages = client.get_outages(outage_type=outage_type, window=window)
    except ThousandEyesAPIError as exc:
        return json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] get_internet_insights failed: %s", exc)
        return json.dumps({"error": f"Unexpected error: {exc}"})

    if not outages:
        return json.dumps({
            "outages": [],
            "outage_type": outage_type,
            "message": f"No {outage_type} outages detected.",
        })

    items, trunc_meta = _truncate_list(
        outages, len(outages),
        hint="Use a shorter window to narrow results.",
    )

    result: Dict[str, Any] = {
        "total_outages": len(outages),
        "outage_type": outage_type,
        "outages": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)
