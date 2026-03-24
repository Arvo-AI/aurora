"""Cloudflare diagnostic tool for the RCA agent.

Provides read-only access to Cloudflare zones, DNS records, analytics,
security events, firewall rules, Workers, load balancers, SSL settings,
and healthchecks to aid root-cause analysis.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

CLOUDFLARE_TIMEOUT = 20
MAX_OUTPUT_SIZE = 2 * 1024 * 1024  # 2 MB

VALID_RESOURCE_TYPES = {
    "zones",
    "dns_records",
    "analytics",
    "firewall_events",
    "firewall_rules",
    "workers",
    "load_balancers",
    "ssl",
    "healthchecks",
}


# ---------------------------------------------------------------------------
# Pydantic args schemas
# ---------------------------------------------------------------------------

class CloudflareQueryArgs(BaseModel):
    """Arguments for query_cloudflare tool."""
    resource_type: str = Field(
        description=(
            "Type of Cloudflare data to query. One of: "
            "'zones' (list zones), "
            "'dns_records' (DNS records for a zone), "
            "'analytics' (traffic/threat/status-code dashboard for a zone), "
            "'firewall_events' (recent WAF/security events for a zone), "
            "'firewall_rules' (active firewall rules for a zone), "
            "'workers' (list Workers scripts), "
            "'load_balancers' (load balancers for a zone), "
            "'ssl' (SSL/TLS mode and certificate status for a zone), "
            "'healthchecks' (configured healthchecks for a zone)."
        )
    )
    zone_id: Optional[str] = Field(
        default=None,
        description="Cloudflare zone ID. Required for all resource_types except 'zones' and 'workers'. Use query_cloudflare(resource_type='zones') first to discover zone IDs.",
    )
    record_type: Optional[str] = Field(
        default=None,
        description="DNS record type filter (A, AAAA, CNAME, MX, TXT, etc.). Only used with resource_type='dns_records'.",
    )
    name: Optional[str] = Field(
        default=None,
        description="DNS record name filter (e.g. 'api.example.com'). Only used with resource_type='dns_records'.",
    )
    since: Optional[str] = Field(
        default=None,
        description="Start time for analytics. Relative minutes as negative int string (e.g. '-1440' for last 24h) or ISO-8601. Only used with resource_type='analytics'.",
    )
    limit: int = Field(
        default=50,
        description="Maximum results to return (default 50).",
    )


class CloudflareListZonesArgs(BaseModel):
    """Arguments for cloudflare_list_zones tool."""
    pass


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _get_cloudflare_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the user's Cloudflare API token and account metadata from Vault."""
    try:
        creds = get_token_data(user_id, "cloudflare")
        if not creds:
            return None
        if not creds.get("api_token"):
            return None
        return creds
    except Exception as exc:
        logger.error(f"[CLOUDFLARE-TOOL] Failed to get credentials: {exc}")
        return None


def is_cloudflare_connected(user_id: str) -> bool:
    """Return True when the user has a valid Cloudflare token stored."""
    return _get_cloudflare_credentials(user_id) is not None


def _get_enabled_zone_ids(user_id: str) -> Optional[List[str]]:
    """Return the list of zone IDs the user explicitly enabled, or None if no preference is stored."""
    try:
        from utils.auth.stateless_auth import get_user_preference
        prefs = get_user_preference(user_id, "cloudflare_zones")
        if not prefs or not isinstance(prefs, list):
            return None
        enabled = [z["id"] for z in prefs if isinstance(z, dict) and z.get("enabled", True)]
        return enabled if enabled else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal query handlers
# ---------------------------------------------------------------------------

def _build_client(creds: Dict[str, Any]):
    from connectors.cloudflare_connector.api_client import CloudflareClient
    return CloudflareClient(creds["api_token"])


def _query_zones(creds: Dict, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    zones = client.list_zones(account_id=creds.get("account_id"))
    return {
        "resource_type": "zones",
        "count": len(zones),
        "results": [
            {
                "id": z.get("id"),
                "name": z.get("name"),
                "status": z.get("status"),
                "paused": z.get("paused"),
                "plan": z.get("plan", {}).get("name"),
                "name_servers": z.get("name_servers"),
            }
            for z in zones
        ],
    }


def _query_dns_records(creds: Dict, zone_id: str, record_type: Optional[str] = None,
                       name: Optional[str] = None, limit: int = 50, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    records = client.list_dns_records(zone_id, record_type=record_type, name=name)[:limit]
    return {
        "resource_type": "dns_records",
        "zone_id": zone_id,
        "count": len(records),
        "results": [
            {
                "id": r.get("id"),
                "type": r.get("type"),
                "name": r.get("name"),
                "content": r.get("content"),
                "proxied": r.get("proxied"),
                "ttl": r.get("ttl"),
            }
            for r in records
        ],
    }


def _query_analytics(creds: Dict, zone_id: str, since: Optional[str] = None, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    dashboard = client.get_zone_analytics(zone_id, since=since or "-1440")

    totals = dashboard.get("totals", {})
    requests_data = totals.get("requests", {})
    bandwidth = totals.get("bandwidth", {})
    threats = totals.get("threats", {})
    pageviews = totals.get("pageviews", {})

    return {
        "resource_type": "analytics",
        "zone_id": zone_id,
        "requests": {
            "total": requests_data.get("all"),
            "cached": requests_data.get("cached"),
            "uncached": requests_data.get("uncached"),
            "ssl_encrypted": requests_data.get("ssl", {}).get("encrypted"),
            "http_status": requests_data.get("http_status", {}),
            "country_top": dict(sorted(
                requests_data.get("country", {}).items(),
                key=lambda x: x[1], reverse=True
            )[:10]) if requests_data.get("country") else {},
        },
        "bandwidth": {
            "total": bandwidth.get("all"),
            "cached": bandwidth.get("cached"),
            "uncached": bandwidth.get("uncached"),
        },
        "threats": {
            "total": threats.get("all"),
            "by_type": threats.get("type", {}),
            "by_country": dict(sorted(
                threats.get("country", {}).items(),
                key=lambda x: x[1], reverse=True
            )[:10]) if threats.get("country") else {},
        },
        "pageviews": pageviews.get("all"),
    }


def _query_firewall_events(creds: Dict, zone_id: str, limit: int = 50, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    events = client.get_firewall_events(zone_id, limit=limit)
    return {
        "resource_type": "firewall_events",
        "zone_id": zone_id,
        "count": len(events),
        "results": [
            {
                "action": e.get("action"),
                "clientIP": e.get("clientIP"),
                "clientRequestHTTPHost": e.get("clientRequestHTTPHost"),
                "clientRequestPath": e.get("clientRequestPath"),
                "clientRequestHTTPMethodName": e.get("clientRequestHTTPMethodName"),
                "ruleId": e.get("ruleId"),
                "source": e.get("source"),
                "userAgent": e.get("userAgent"),
                "datetime": e.get("datetime"),
            }
            for e in events
        ],
    }


def _query_firewall_rules(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    rules = client.list_firewall_rules(zone_id)
    return {
        "resource_type": "firewall_rules",
        "zone_id": zone_id,
        "count": len(rules),
        "results": [
            {
                "id": r.get("id"),
                "description": r.get("description"),
                "action": r.get("action"),
                "paused": r.get("paused"),
                "filter_expression": r.get("filter", {}).get("expression"),
            }
            for r in rules
        ],
    }


def _query_workers(creds: Dict, **_kw) -> Dict[str, Any]:
    account_id = creds.get("account_id")
    if not account_id:
        return {"resource_type": "workers", "error": "No account_id available", "results": []}
    client = _build_client(creds)
    workers = client.list_workers(account_id)
    return {
        "resource_type": "workers",
        "count": len(workers),
        "results": [
            {
                "id": w.get("id"),
                "created_on": w.get("created_on"),
                "modified_on": w.get("modified_on"),
                "etag": w.get("etag"),
            }
            for w in workers
        ],
    }


def _query_load_balancers(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    lbs = client.list_load_balancers(zone_id)
    return {
        "resource_type": "load_balancers",
        "zone_id": zone_id,
        "count": len(lbs),
        "results": [
            {
                "id": lb.get("id"),
                "name": lb.get("name"),
                "enabled": lb.get("enabled"),
                "default_pools": lb.get("default_pools"),
                "fallback_pool": lb.get("fallback_pool"),
                "proxied": lb.get("proxied"),
                "ttl": lb.get("ttl"),
                "session_affinity": lb.get("session_affinity"),
            }
            for lb in lbs
        ],
    }


def _query_ssl(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    ssl_mode = client.get_ssl_settings(zone_id)
    try:
        verification = client.get_ssl_verification(zone_id)
    except Exception:
        verification = []
    return {
        "resource_type": "ssl",
        "zone_id": zone_id,
        "ssl_mode": ssl_mode.get("value"),
        "certificates": [
            {
                "hostname": v.get("hostname"),
                "status": v.get("certificate_status"),
                "validation_type": v.get("validation_type"),
            }
            for v in verification
        ],
    }


def _query_healthchecks(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    checks = client.list_healthchecks(zone_id)
    return {
        "resource_type": "healthchecks",
        "zone_id": zone_id,
        "count": len(checks),
        "results": [
            {
                "id": hc.get("id"),
                "name": hc.get("name"),
                "status": hc.get("status"),
                "type": hc.get("type"),
                "address": hc.get("address"),
                "suspended": hc.get("suspended"),
                "failure_reason": hc.get("failure_reason"),
                "interval": hc.get("interval"),
                "retries": hc.get("retries"),
                "timeout": hc.get("timeout"),
            }
            for hc in checks
        ],
    }


_HANDLERS = {
    "zones": _query_zones,
    "dns_records": _query_dns_records,
    "analytics": _query_analytics,
    "firewall_events": _query_firewall_events,
    "firewall_rules": _query_firewall_rules,
    "workers": _query_workers,
    "load_balancers": _query_load_balancers,
    "ssl": _query_ssl,
    "healthchecks": _query_healthchecks,
}

_ZONE_REQUIRED = {"dns_records", "analytics", "firewall_events", "firewall_rules",
                   "load_balancers", "ssl", "healthchecks"}


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def _truncate_results(results: list) -> tuple:
    truncated: list = []
    total_size = 0
    for item in results:
        item_str = json.dumps(item)
        if total_size + len(item_str) > MAX_OUTPUT_SIZE:
            return truncated, True
        truncated.append(item)
        total_size += len(item_str)
    return truncated, False


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def query_cloudflare(
    resource_type: str,
    zone_id: Optional[str] = None,
    record_type: Optional[str] = None,
    name: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Cloudflare for zones, DNS, analytics, security events, and more."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_cloudflare_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Cloudflare not connected. Please connect Cloudflare first."})

    resource_type = resource_type.lower().strip()
    handler = _HANDLERS.get(resource_type)
    if not handler:
        return json.dumps({
            "error": f"Invalid resource_type '{resource_type}'. Must be one of: {', '.join(sorted(VALID_RESOURCE_TYPES))}"
        })

    if resource_type in _ZONE_REQUIRED and not zone_id:
        return json.dumps({
            "error": f"zone_id is required for resource_type='{resource_type}'. Use query_cloudflare(resource_type='zones') first to discover zone IDs."
        })

    limit = min(max(limit, 1), 500)
    logger.info("[CLOUDFLARE-TOOL] user=%s resource=%s zone=%s", user_id, resource_type, zone_id or "all")

    try:
        result = handler(
            creds,
            zone_id=zone_id,
            record_type=record_type,
            name=name,
            since=since,
            limit=limit,
        )
        result["success"] = True

        results_list = result.get("results", [])
        if results_list:
            truncated_results, was_truncated = _truncate_results(results_list)
            if was_truncated:
                result["results"] = truncated_results
                result["truncated"] = True
                result["note"] = f"Results truncated from {len(results_list)} to {len(truncated_results)} due to size limit."
                result["count"] = len(truncated_results)

        return json.dumps(result)

    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 401:
            return json.dumps({"error": "Cloudflare authentication failed. Token may be expired or revoked."})
        if status == 403:
            return json.dumps({"error": "Token lacks the required permission for this resource type."})
        if status == 404:
            return json.dumps({"error": f"Resource not found. Verify zone_id='{zone_id}' is correct."})
        body = exc.response.text[:200] if exc.response is not None else ""
        return json.dumps({"error": f"Cloudflare API error ({status}): {body}"})
    except requests.exceptions.Timeout:
        return json.dumps({"error": "Request timed out. Try again or narrow the query."})
    except requests.exceptions.RequestException as exc:
        logger.error(f"[CLOUDFLARE-TOOL] Request failed: {exc}")
        return json.dumps({"error": f"Request failed: {str(exc)}"})
    except Exception as exc:
        logger.error(f"[CLOUDFLARE-TOOL] Unexpected error: {exc}")
        return json.dumps({"error": f"Unexpected error: {str(exc)}"})


def cloudflare_list_zones(
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Quick convenience tool to list all Cloudflare zones (no parameters needed)."""
    return query_cloudflare(resource_type="zones", user_id=user_id)
