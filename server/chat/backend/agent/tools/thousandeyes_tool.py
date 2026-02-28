"""ThousandEyes network intelligence tools for the RCA agent.

Provides LLM-callable tools that query a user's ThousandEyes account for
tests, test results, alerts, agents, and Internet Insights outage data.
Credentials are loaded from Vault via ``get_token_data(user_id, "thousandeyes")``.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

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

_ERROR_USER = json.dumps({"error": "User context not available"})
_ERROR_NOT_CONNECTED = json.dumps({"error": NOT_CONNECTED_MSG})


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
# Shared tool helpers - reduce boilerplate across all tool functions
# ---------------------------------------------------------------------------

def _call_api(
    user_id: Optional[str],
    tool_name: str,
    api_fn: Callable[[ThousandEyesClient], Any],
) -> Tuple[bool, Any]:
    """Acquire a client and call *api_fn*, handling all common error paths.

    Returns ``(True, data)`` on success, or ``(False, error_json_str)`` on
    failure.  Every tool function can delegate its client-acquisition and
    error-handling boilerplate to this single helper.
    """
    if not user_id:
        return False, _ERROR_USER

    client = _build_client(user_id)
    if not client:
        return False, _ERROR_NOT_CONNECTED

    try:
        data = api_fn(client)
        return True, data
    except ThousandEyesAPIError as exc:
        return False, json.dumps({"error": f"ThousandEyes API error: {exc}"})
    except Exception as exc:
        logger.error("[THOUSANDEYES-TOOL] %s failed: %s", tool_name, exc)
        return False, json.dumps({"error": "An unexpected error occurred"})


def _summarize_list(
    items: List[Dict[str, Any]],
    fields: Tuple[str, ...],
    result_key: str,
    total_key: str,
    hint: str = "Use filters to narrow results.",
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Project *items* to *fields*, truncate, and return JSON.

    Shared by every tool that returns a list of summarised records.
    """
    summaries = [{k: item.get(k) for k in fields} for item in items]
    truncated, trunc_meta = _truncate_list(summaries, len(summaries), hint=hint)

    result: Dict[str, Any] = {total_key: len(summaries), result_key: truncated}
    if extra:
        result.update(extra)
    result.update(trunc_meta)
    return _safe_json(result)


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


class ThousandEyesGetTestDetailArgs(BaseModel):
    test_id: str = Field(description="The ThousandEyes test ID to get full details for.")


class ThousandEyesGetTestResultsArgs(BaseModel):
    test_id: str = Field(description="The ThousandEyes test ID to get results for.")
    result_type: str = Field(
        default="network",
        description=(
            "Type of results to fetch: 'network' (latency, loss, jitter), "
            "'http' (response time, availability), 'path-vis' (hop-by-hop trace), "
            "'dns' (DNS resolution), 'bgp' (BGP routes), 'page-load' (full waterfall), "
            "'web-transactions' (scripted browser), 'ftp' (FTP results), 'api' (API test), "
            "'sip' (SIP/VoIP), 'voice' (MOS, jitter), 'dns-trace' (trace chain), "
            "'dnssec' (DNSSEC validation)."
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


class ThousandEyesGetAlertRulesArgs(BaseModel):
    pass


class ThousandEyesGetDashboardsArgs(BaseModel):
    dashboard_id: Optional[str] = Field(
        default=None,
        description="Optional dashboard ID to get a specific dashboard with its widgets. Leave empty to list all dashboards.",
    )


class ThousandEyesGetDashboardWidgetArgs(BaseModel):
    dashboard_id: str = Field(description="The dashboard ID.")
    widget_id: str = Field(description="The widget ID within the dashboard.")
    window: Optional[str] = Field(
        default=None,
        description="Time window (e.g. '1h', '6h', '1d'). Defaults to the widget's configured window.",
    )


class ThousandEyesGetEndpointAgentsArgs(BaseModel):
    pass


class ThousandEyesGetBGPMonitorsArgs(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Field tuples for list summarization
# ---------------------------------------------------------------------------

_TEST_FIELDS = ("testId", "testName", "type", "enabled", "interval", "server", "url", "createdDate")
_AGENT_FIELDS = ("agentId", "agentName", "agentType", "countryId", "location", "enabled", "agentState", "ipAddresses", "lastSeen")
_RULE_FIELDS = ("ruleId", "ruleName", "expression", "alertType", "severity", "default", "testIds", "notifyOnClear")
_DASH_FIELDS = ("dashboardId", "title", "description", "isBuiltIn", "createdBy", "modifiedDate")
_EP_FIELDS = ("agentId", "agentName", "computerName", "osVersion", "platform", "location", "publicIP", "lastSeen", "status", "vpnProfiles")
_MON_FIELDS = ("monitorId", "monitorName", "monitorType", "ipAddress", "network", "countryId")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def thousandeyes_list_tests(
    *,
    user_id: Optional[str] = None,
    test_type: Optional[str] = None,
    **kwargs,
) -> str:
    """List all configured ThousandEyes tests."""
    ok, data = _call_api(user_id, "list_tests", lambda c: c.get_tests(test_type=test_type))
    if not ok:
        return data

    if not data:
        filter_desc = f" (type={test_type})" if test_type else ""
        return json.dumps({"tests": [], "message": f"No tests found{filter_desc}."})

    return _summarize_list(
        data, _TEST_FIELDS,
        result_key="tests", total_key="total_tests",
        hint="Use test_type filter to narrow results.",
    )


def thousandeyes_get_test_detail(
    *,
    user_id: Optional[str] = None,
    test_id: str = "",
    **kwargs,
) -> str:
    """Get full configuration details for a single ThousandEyes test."""
    if not test_id:
        return json.dumps({"error": "test_id is required"})

    ok, data = _call_api(user_id, "get_test_detail", lambda c: c.get_test(test_id))
    if not ok:
        return data

    if not data:
        return json.dumps({"test_id": test_id, "message": "Test not found."})

    return _safe_json(data)


def thousandeyes_get_test_results(
    *,
    user_id: Optional[str] = None,
    test_id: str = "",
    result_type: str = "network",
    window: Optional[str] = None,
    **kwargs,
) -> str:
    """Get results for a specific ThousandEyes test."""
    if not test_id:
        return json.dumps({"error": "test_id is required"})

    ok, data = _call_api(
        user_id, "get_test_results",
        lambda c: c.get_test_results(test_id, result_type=result_type, window=window),
    )
    if not ok:
        return data

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
    **kwargs,
) -> str:
    """Get ThousandEyes alerts."""
    ok, alerts = _call_api(
        user_id, "get_alerts",
        lambda c: c.get_alerts(state=state, severity=severity, window=window),
    )
    if not ok:
        return alerts

    if not alerts:
        filter_parts = []
        if state:
            filter_parts.append(f"state={state}")
        if severity:
            filter_parts.append(f"severity={severity}")
        filter_desc = f" ({', '.join(filter_parts)})" if filter_parts else ""
        return json.dumps({"alerts": [], "message": f"No alerts found{filter_desc}."})

    _ALERT_FIELDS = (
        "alertId", "testId", "testName", "alertType", "alertState",
        "alertSeverity", "startDate", "endDate", "violationCount", "ruleExpression",
    )
    summaries = [{k: a.get(k) for k in _ALERT_FIELDS} for a in alerts]
    # Include a compact agent sub-summary (up to 5 agents per alert)
    for summary, alert in zip(summaries, alerts):
        summary["agents"] = [
            {"agentName": agent.get("agentName"), "active": agent.get("active")}
            for agent in (alert.get("agents") or [])[:5]
        ]

    truncated, trunc_meta = _truncate_list(
        summaries, len(summaries),
        hint="Use state/severity filters to narrow results.",
    )

    active_count = sum(1 for a in summaries if a.get("alertState") == "active")

    result: Dict[str, Any] = {
        "total_alerts": len(summaries),
        "active_alerts": active_count,
        "alerts": truncated,
    }
    result.update(trunc_meta)
    return _safe_json(result)


def thousandeyes_get_alert_rules(
    *,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List all ThousandEyes alert rule definitions."""
    ok, rules = _call_api(user_id, "get_alert_rules", lambda c: c.get_alert_rules())
    if not ok:
        return rules

    if not rules:
        return json.dumps({"alert_rules": [], "message": "No alert rules configured."})

    return _summarize_list(rules, _RULE_FIELDS, result_key="alert_rules", total_key="total_rules")


def thousandeyes_get_agents(
    *,
    user_id: Optional[str] = None,
    agent_type: Optional[str] = None,
    **kwargs,
) -> str:
    """List ThousandEyes agents and their status."""
    ok, agents = _call_api(user_id, "get_agents", lambda c: c.get_agents(agent_type=agent_type))
    if not ok:
        return agents

    if not agents:
        filter_desc = f" (type={agent_type})" if agent_type else ""
        return json.dumps({"agents": [], "message": f"No agents found{filter_desc}."})

    return _summarize_list(
        agents, _AGENT_FIELDS,
        result_key="agents", total_key="total_agents",
        hint="Use agent_type filter ('cloud' or 'enterprise') to narrow results.",
    )


def thousandeyes_get_endpoint_agents(
    *,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List ThousandEyes endpoint agents (employee devices)."""
    ok, agents = _call_api(user_id, "get_endpoint_agents", lambda c: c.get_endpoint_agents())
    if not ok:
        return agents

    if not agents:
        return json.dumps({"agents": [], "message": "No endpoint agents found."})

    return _summarize_list(agents, _EP_FIELDS, result_key="agents", total_key="total_endpoint_agents")


def thousandeyes_get_internet_insights(
    *,
    user_id: Optional[str] = None,
    outage_type: str = "network",
    window: Optional[str] = None,
    **kwargs,
) -> str:
    """Get Internet Insights outage data from ThousandEyes."""
    ok, outages = _call_api(
        user_id, "get_internet_insights",
        lambda c: c.get_outages(outage_type=outage_type, window=window),
    )
    if not ok:
        return outages

    if not outages:
        return json.dumps({
            "outages": [],
            "outage_type": outage_type,
            "message": f"No {outage_type} outages detected.",
        })

    truncated, trunc_meta = _truncate_list(
        outages, len(outages),
        hint="Use a shorter window to narrow results.",
    )

    result: Dict[str, Any] = {
        "total_outages": len(outages),
        "outage_type": outage_type,
        "outages": truncated,
    }
    result.update(trunc_meta)
    return _safe_json(result)


def thousandeyes_get_dashboards(
    *,
    user_id: Optional[str] = None,
    dashboard_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List ThousandEyes dashboards, or get a specific dashboard with its widgets."""
    if dashboard_id:
        ok, data = _call_api(
            user_id, "get_dashboards",
            lambda c: c.get_dashboard(dashboard_id),
        )
        if not ok:
            return data
        return _safe_json(data)

    ok, dashboards = _call_api(user_id, "get_dashboards", lambda c: c.get_dashboards())
    if not ok:
        return dashboards

    if not dashboards:
        return json.dumps({"dashboards": [], "message": "No dashboards found."})

    return _summarize_list(dashboards, _DASH_FIELDS, result_key="dashboards", total_key="total_dashboards")


def thousandeyes_get_dashboard_widget(
    *,
    user_id: Optional[str] = None,
    dashboard_id: str = "",
    widget_id: str = "",
    window: Optional[str] = None,
    **kwargs,
) -> str:
    """Get data for a specific widget within a ThousandEyes dashboard."""
    if not dashboard_id or not widget_id:
        return json.dumps({"error": "dashboard_id and widget_id are required"})

    ok, data = _call_api(
        user_id, "get_dashboard_widget",
        lambda c: c.get_dashboard_widget(dashboard_id, widget_id, window=window),
    )
    if not ok:
        return data

    if not data:
        return json.dumps({"dashboard_id": dashboard_id, "widget_id": widget_id, "message": "No widget data found."})

    return _safe_json(data)


def thousandeyes_get_bgp_monitors(
    *,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List ThousandEyes BGP monitoring points."""
    ok, monitors = _call_api(user_id, "get_bgp_monitors", lambda c: c.get_bgp_monitors())
    if not ok:
        return monitors

    if not monitors:
        return json.dumps({"monitors": [], "message": "No BGP monitors found."})

    return _summarize_list(monitors, _MON_FIELDS, result_key="monitors", total_key="total_monitors")
