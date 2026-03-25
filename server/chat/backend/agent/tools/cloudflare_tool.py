"""Cloudflare diagnostic and remediation tools for the RCA agent.

Provides read access to Cloudflare zones, DNS records, analytics,
security events, firewall rules, zone settings, page rules, Workers,
load balancers, SSL settings, and healthchecks to aid root-cause analysis.

Remediation actions are exposed via the unified ``cloudflare_action`` tool:
cache purge, security level changes, development mode, DNS record updates,
and firewall rule toggling.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 2 * 1024 * 1024  # 2 MB

VALID_RESOURCE_TYPES = {
    "dns_records",
    "analytics",
    "firewall_events",
    "firewall_rules",
    "rate_limits",
    "workers",
    "load_balancers",
    "ssl",
    "healthchecks",
    "zone_settings",
    "page_rules",
}


# ---------------------------------------------------------------------------
# Pydantic args schemas
# ---------------------------------------------------------------------------

class CloudflareQueryArgs(BaseModel):
    """Arguments for query_cloudflare tool."""
    resource_type: str = Field(
        description=(
            "Type of Cloudflare data to query. One of: "
            "'dns_records' (DNS records for a zone), "
            "'analytics' (traffic/threat/status-code dashboard for a zone), "
            "'firewall_events' (recent WAF/security events for a zone), "
            "'firewall_rules' (active firewall rules for a zone), "
            "'rate_limits' (rate limiting rules for a zone), "
            "'workers' (list Workers scripts), "
            "'load_balancers' (load balancers for a zone), "
            "'ssl' (SSL/TLS mode and certificate status for a zone), "
            "'healthchecks' (configured healthchecks for a zone), "
            "'zone_settings' (all zone settings: security level, caching, dev mode, etc.), "
            "'page_rules' (URL-based redirects, forwarding, cache overrides). "
            "Use cloudflare_list_zones() first to discover zone IDs."
        )
    )
    zone_id: Optional[str] = Field(
        default=None,
        description="Cloudflare zone ID. Required for all resource_types except 'workers'. Use cloudflare_list_zones() first to discover zone IDs.",
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
        description="Start time for analytics/firewall_events. Relative minutes as negative int string (e.g. '-1440' for last 24h) or ISO-8601. Defaults to last 24h.",
    )
    until: Optional[str] = Field(
        default=None,
        description="End time for analytics/firewall_events. ISO-8601 string. Defaults to now.",
    )
    limit: int = Field(
        default=50,
        description="Maximum results to return (default 50). For analytics with limit > 1, returns time-series buckets instead of a single aggregate.",
    )


class CloudflareListZonesArgs(BaseModel):
    """Arguments for cloudflare_list_zones tool."""
    pass


class CloudflareActionArgs(BaseModel):
    """Arguments for cloudflare_action remediation tool."""
    action_type: str = Field(
        description=(
            "Remediation action to perform. One of: "
            "'purge_cache' (clear cached content — pass 'files' for targeted purge or omit for full purge), "
            "'security_level' (set zone security level — pass 'value': 'under_attack','high','medium','low','essentially_off'), "
            "'development_mode' (bypass cache for debugging — pass 'value': 'on' or 'off'), "
            "'dns_update' (update a DNS record — requires 'record_id', optional 'content','proxied','ttl'), "
            "'toggle_firewall_rule' (enable/disable a firewall rule — requires 'rule_id' and 'paused')."
        )
    )
    zone_id: str = Field(
        description="Cloudflare zone ID. Use cloudflare_list_zones() first to discover zone IDs.",
    )
    value: Optional[str] = Field(
        default=None,
        description=(
            "Setting value. Used by: security_level ('under_attack','high','medium','low','essentially_off'), "
            "development_mode ('on','off')."
        ),
    )
    record_id: Optional[str] = Field(
        default=None,
        description="DNS record ID. Required for action_type='dns_update'. Get IDs from query_cloudflare(resource_type='dns_records').",
    )
    rule_id: Optional[str] = Field(
        default=None,
        description="Firewall rule ID. Required for action_type='toggle_firewall_rule'. Get IDs from query_cloudflare(resource_type='firewall_rules').",
    )
    content: Optional[str] = Field(
        default=None,
        description="New DNS record content (IP address or hostname). Used by dns_update.",
    )
    proxied: Optional[bool] = Field(
        default=None,
        description="Whether to proxy traffic through Cloudflare. Used by dns_update.",
    )
    ttl: Optional[int] = Field(
        default=None,
        description="DNS record TTL in seconds (1 = auto). Used by dns_update.",
    )
    paused: Optional[bool] = Field(
        default=None,
        description="True to disable (pause) a firewall rule, False to enable it. Required for toggle_firewall_rule.",
    )
    files: Optional[List[str]] = Field(
        default=None,
        description=(
            "URLs to purge from cache (e.g. ['https://example.com/styles.css']). "
            "Omit to purge the ENTIRE zone cache. Used by purge_cache."
        ),
    )


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _get_cloudflare_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the user's Cloudflare API token and account metadata from Vault."""
    try:
        creds = get_token_data(user_id, "cloudflare")
        if not creds or not creds.get("api_token"):
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


def _query_analytics(creds: Dict, zone_id: str, since: Optional[str] = None,
                     until: Optional[str] = None, limit: int = 50, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    query_limit = min(limit, 100)
    groups = client.get_zone_analytics(
        zone_id, since=since or "-1440", until=until, limit=query_limit,
    )

    if not groups:
        return {
            "resource_type": "analytics",
            "zone_id": zone_id,
            "note": "No analytics data available for this time range.",
        }

    def _format_group(group: Dict) -> Dict[str, Any]:
        sums = group.get("sum", {})
        uniq = group.get("uniq", {})
        dims = group.get("dimensions", {})

        total_requests = sums.get("requests", 0)
        cached_requests = sums.get("cachedRequests", 0)
        uncached = (total_requests - cached_requests) if total_requests and cached_requests else 0

        country_map = sums.get("countryMap", [])
        country_requests = {c["clientCountryName"]: c["requests"] for c in country_map if c.get("clientCountryName")}
        country_threats = {c["clientCountryName"]: c["threats"] for c in country_map if c.get("threats")}

        status_map = sums.get("responseStatusMap", [])
        http_status = {str(s["edgeResponseStatus"]): s["requests"] for s in status_map if s.get("edgeResponseStatus") is not None}

        threat_map = sums.get("threatPathingMap", [])
        threat_types = {t["threatPathingName"]: t["requests"] for t in threat_map if t.get("threatPathingName")}

        content_type_map = sums.get("contentTypeMap", [])
        content_types = {c["edgeResponseContentTypeName"]: {"requests": c["requests"], "bytes": c["bytes"]}
                         for c in content_type_map if c.get("edgeResponseContentTypeName")}

        http_version_map = sums.get("clientHTTPVersionMap", [])
        http_versions = {v["clientHTTPProtocol"]: v["requests"]
                         for v in http_version_map if v.get("clientHTTPProtocol")}

        ssl_map = sums.get("clientSSLMap", [])
        ssl_versions = {s["clientSSLProtocol"]: s["requests"]
                        for s in ssl_map if s.get("clientSSLProtocol")}

        ip_class_map = sums.get("ipClassMap", [])
        ip_classes = {i["ipType"]: i["requests"]
                      for i in ip_class_map if i.get("ipType")}

        result: Dict[str, Any] = {
            "requests": {
                "total": total_requests,
                "cached": cached_requests,
                "uncached": uncached,
                "ssl_encrypted": sums.get("encryptedRequests"),
                "http_status": http_status,
                "country_top": dict(sorted(
                    country_requests.items(),
                    key=lambda x: x[1], reverse=True
                )[:10]) if country_requests else {},
            },
            "bandwidth": {
                "total": sums.get("bytes"),
                "cached": sums.get("cachedBytes"),
                "uncached": (sums.get("bytes", 0) - sums.get("cachedBytes", 0)) if sums.get("bytes") else 0,
            },
            "threats": {
                "total": sums.get("threats"),
                "by_type": threat_types,
                "by_country": dict(sorted(
                    country_threats.items(),
                    key=lambda x: x[1], reverse=True
                )[:10]) if country_threats else {},
            },
            "pageviews": sums.get("pageViews"),
            "unique_visitors": uniq.get("uniques"),
        }
        if content_types:
            result["content_types"] = content_types
        if http_versions:
            result["http_versions"] = http_versions
        if ssl_versions:
            result["ssl_versions"] = ssl_versions
        if ip_classes:
            result["ip_classification"] = ip_classes
        if dims.get("datetime"):
            result["datetime"] = dims["datetime"]
        return result

    if len(groups) == 1:
        formatted = _format_group(groups[0])
        formatted["resource_type"] = "analytics"
        formatted["zone_id"] = zone_id
        return formatted

    return {
        "resource_type": "analytics",
        "zone_id": zone_id,
        "bucket_count": len(groups),
        "time_series": [_format_group(g) for g in groups],
    }


def _query_firewall_events(creds: Dict, zone_id: str, limit: int = 50,
                           since: Optional[str] = None, until: Optional[str] = None,
                           **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    events = client.get_firewall_events(zone_id, limit=limit, since=since, until=until)
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


def _query_firewall_rules(creds: Dict, zone_id: str, limit: int = 50, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    rules = client.list_firewall_rules(zone_id)[:limit]
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


def _query_rate_limits(creds: Dict, zone_id: str, limit: int = 50, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    rules = client.list_rate_limits(zone_id)[:limit]
    return {
        "resource_type": "rate_limits",
        "zone_id": zone_id,
        "count": len(rules),
        "results": [
            {
                "id": r.get("id"),
                "description": r.get("description"),
                "disabled": r.get("disabled"),
                "threshold": r.get("threshold"),
                "period": r.get("period"),
                "action": r.get("action", {}).get("mode"),
                "action_timeout": r.get("action", {}).get("timeout"),
                "match_url": r.get("match", {}).get("request", {}).get("url"),
                "match_methods": r.get("match", {}).get("request", {}).get("methods"),
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


def _query_zone_settings(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    settings = client.get_zone_settings(zone_id)
    key_settings = {}
    for s in settings:
        sid = s.get("id")
        if sid:
            key_settings[sid] = s.get("value")
    return {
        "resource_type": "zone_settings",
        "zone_id": zone_id,
        "count": len(settings),
        "key_settings": {
            "security_level": key_settings.get("security_level"),
            "ssl": key_settings.get("ssl"),
            "cache_level": key_settings.get("cache_level"),
            "development_mode": key_settings.get("development_mode"),
            "always_online": key_settings.get("always_online"),
            "browser_check": key_settings.get("browser_check"),
            "challenge_ttl": key_settings.get("challenge_ttl"),
            "min_tls_version": key_settings.get("min_tls_version"),
            "automatic_https_rewrites": key_settings.get("automatic_https_rewrites"),
            "minify": key_settings.get("minify"),
            "waf": key_settings.get("waf"),
        },
        "all_settings": {s.get("id"): s.get("value") for s in settings if s.get("id")},
    }


def _query_page_rules(creds: Dict, zone_id: str, **_kw) -> Dict[str, Any]:
    client = _build_client(creds)
    try:
        rules = client.list_page_rules(zone_id)
    except Exception as exc:
        import requests as _requests
        if isinstance(exc, _requests.exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else 0
            if status == 400:
                body = exc.response.text[:300] if exc.response is not None else ""
                if "account owned tokens" in body.lower():
                    return {
                        "resource_type": "page_rules",
                        "zone_id": zone_id,
                        "count": 0,
                        "results": [],
                        "note": "Page Rules API does not support account-owned tokens. Use a user-owned API token to access page rules.",
                    }
        raise
    return {
        "resource_type": "page_rules",
        "zone_id": zone_id,
        "count": len(rules),
        "results": [
            {
                "id": r.get("id"),
                "status": r.get("status"),
                "priority": r.get("priority"),
                "targets": [
                    t.get("constraint", {}).get("value")
                    for t in r.get("targets", [])
                ],
                "actions": [
                    {"id": a.get("id"), "value": a.get("value")}
                    for a in r.get("actions", [])
                ],
            }
            for r in rules
        ],
    }


_HANDLERS = {
    "dns_records": _query_dns_records,
    "analytics": _query_analytics,
    "firewall_events": _query_firewall_events,
    "firewall_rules": _query_firewall_rules,
    "rate_limits": _query_rate_limits,
    "workers": _query_workers,
    "load_balancers": _query_load_balancers,
    "ssl": _query_ssl,
    "healthchecks": _query_healthchecks,
    "zone_settings": _query_zone_settings,
    "page_rules": _query_page_rules,
}

_ZONE_REQUIRED = {"dns_records", "analytics", "firewall_events", "firewall_rules",
                   "rate_limits", "load_balancers", "ssl", "healthchecks",
                   "zone_settings", "page_rules"}


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
    until: Optional[str] = None,
    limit: int = 50,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Cloudflare for DNS, analytics, security events, and more."""
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
            "error": f"zone_id is required for resource_type='{resource_type}'. Use cloudflare_list_zones() first to discover zone IDs."
        })

    if zone_id and resource_type in _ZONE_REQUIRED:
        allowed = _get_enabled_zone_ids(user_id)
        if allowed is not None and zone_id not in allowed:
            return json.dumps({
                "error": f"Zone '{zone_id}' is not in your enabled zones. "
                         "Enable it on the Cloudflare settings page or use cloudflare_list_zones() to see available zones."
            })

    limit = min(max(limit, 1), 100) #Limit between 1 and 100
    logger.info("[CLOUDFLARE-TOOL] user=%s resource=%s zone=%s", user_id, resource_type, zone_id or "all")

    try:
        result = handler(
            creds,
            zone_id=zone_id,
            record_type=record_type,
            name=name,
            since=since,
            until=until,
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

    except Exception as exc:
        import requests as _requests
        if isinstance(exc, _requests.exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else "unknown"
            if status == 401:
                return json.dumps({"error": "Cloudflare authentication failed. Token may be expired or revoked."})
            if status == 403:
                return json.dumps({"error": "Token lacks the required permission for this resource type."})
            if status == 404:
                return json.dumps({"error": f"Resource not found. Verify zone_id='{zone_id}' is correct."})
            body = exc.response.text[:200] if exc.response is not None else ""
            return json.dumps({"error": f"Cloudflare API error ({status}): {body}"})
        if isinstance(exc, _requests.exceptions.Timeout):
            return json.dumps({"error": "Request timed out. Try again or narrow the query."})
        if isinstance(exc, _requests.exceptions.RequestException):
            logger.error(f"[CLOUDFLARE-TOOL] Request failed: {exc}")
            return json.dumps({"error": f"Request failed: {str(exc)}"})
        logger.error(f"[CLOUDFLARE-TOOL] Unexpected error: {exc}")
        return json.dumps({"error": f"Unexpected error: {str(exc)}"})


def cloudflare_list_zones(
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Quick convenience tool to list all Cloudflare zones (no parameters needed)."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_cloudflare_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Cloudflare not connected. Please connect Cloudflare first."})

    try:
        result = _query_zones(creds)
        allowed = _get_enabled_zone_ids(user_id)
        if allowed is not None:
            result["results"] = [z for z in result["results"] if z.get("id") in allowed]
            result["count"] = len(result["results"])
        result["success"] = True
        return json.dumps(result)
    except Exception as exc:
        logger.error(f"[CLOUDFLARE-TOOL] Failed to list zones: {exc}")
        return json.dumps({"error": f"Failed to list zones: {str(exc)}"})


# ---------------------------------------------------------------------------
# Remediation action dispatch
# ---------------------------------------------------------------------------

VALID_ACTION_TYPES = {
    "purge_cache",
    "security_level",
    "development_mode",
    "dns_update",
    "toggle_firewall_rule",
}

_SECURITY_LEVELS = {"essentially_off", "low", "medium", "high", "under_attack"}


def _action_purge_cache(client, zone_id: str, **kw) -> Dict[str, Any]:
    files = kw.get("files")
    result = client.purge_cache(zone_id, files=files)
    purge_id = result.get("id", "unknown")
    if files:
        return {
            "action": "purge_cache",
            "zone_id": zone_id,
            "purged_files": len(files),
            "purge_id": purge_id,
            "message": f"Successfully purged {len(files)} file(s) from cache.",
        }
    return {
        "action": "purge_cache",
        "zone_id": zone_id,
        "purge_everything": True,
        "purge_id": purge_id,
        "message": "Successfully purged entire cache for the zone. Edge nodes may take a few minutes to clear.",
    }


def _action_security_level(client, zone_id: str, **kw) -> Dict[str, Any]:
    value = (kw.get("value") or "").lower().strip()
    if value not in _SECURITY_LEVELS:
        return {"error": f"Invalid security level '{value}'. Must be one of: {', '.join(sorted(_SECURITY_LEVELS))}"}
    result = client.set_security_level(zone_id, value)
    return {
        "action": "security_level",
        "zone_id": zone_id,
        "new_value": result.get("value", value),
        "message": f"Security level set to '{value}'." + (
            " Zone is now in Under Attack Mode — all visitors will see a JS challenge page."
            if value == "under_attack" else ""
        ),
    }


def _action_development_mode(client, zone_id: str, **kw) -> Dict[str, Any]:
    value = (kw.get("value") or "").lower().strip()
    if value not in ("on", "off"):
        return {"error": "Invalid value. Must be 'on' or 'off'."}
    result = client.set_development_mode(zone_id, value)
    return {
        "action": "development_mode",
        "zone_id": zone_id,
        "new_value": result.get("value", value),
        "message": f"Development mode turned {value}." + (
            " Cache is now bypassed — all requests go to origin. Auto-expires after 3 hours."
            if value == "on" else " Caching is active again."
        ),
    }


def _action_dns_update(client, zone_id: str, **kw) -> Dict[str, Any]:
    record_id = kw.get("record_id")
    if not record_id:
        return {"error": "record_id is required for dns_update. Use query_cloudflare(resource_type='dns_records') to find record IDs."}
    content = kw.get("content")
    proxied = kw.get("proxied")
    ttl = kw.get("ttl")
    if content is None and proxied is None and ttl is None:
        return {"error": "At least one of content, proxied, or ttl must be provided."}
    result = client.update_dns_record(zone_id, record_id,
                                      content=content, proxied=proxied, ttl=ttl)
    changes = []
    if content is not None:
        changes.append(f"content='{content}'")
    if proxied is not None:
        changes.append(f"proxied={proxied}")
    if ttl is not None:
        changes.append(f"ttl={ttl}")
    return {
        "action": "dns_update",
        "zone_id": zone_id,
        "record_id": record_id,
        "updated_record": {
            "name": result.get("name"),
            "type": result.get("type"),
            "content": result.get("content"),
            "proxied": result.get("proxied"),
            "ttl": result.get("ttl"),
        },
        "message": f"DNS record updated: {', '.join(changes)}.",
    }


def _action_toggle_firewall_rule(client, zone_id: str, **kw) -> Dict[str, Any]:
    rule_id = kw.get("rule_id")
    paused = kw.get("paused")
    if not rule_id:
        return {"error": "rule_id is required. Use query_cloudflare(resource_type='firewall_rules') to find rule IDs."}
    if paused is None:
        return {"error": "paused is required. Set to true to disable the rule or false to enable it."}
    result = client.update_firewall_rule_paused(zone_id, rule_id, paused)
    state = "disabled (paused)" if paused else "enabled (active)"
    return {
        "action": "toggle_firewall_rule",
        "zone_id": zone_id,
        "rule_id": rule_id,
        "paused": result.get("paused", paused),
        "description": result.get("description"),
        "message": f"Firewall rule {state}.",
    }


_ACTION_HANDLERS = {
    "purge_cache": _action_purge_cache,
    "security_level": _action_security_level,
    "development_mode": _action_development_mode,
    "dns_update": _action_dns_update,
    "toggle_firewall_rule": _action_toggle_firewall_rule,
}

_ACTION_PERMISSIONS = {
    "purge_cache": "Cache Purge",
    "security_level": "Zone Settings Write",
    "development_mode": "Zone Settings Write",
    "dns_update": "DNS Write",
    "toggle_firewall_rule": "Firewall Services Write",
}


def cloudflare_action(
    action_type: str,
    zone_id: str,
    value: Optional[str] = None,
    record_id: Optional[str] = None,
    rule_id: Optional[str] = None,
    content: Optional[str] = None,
    proxied: Optional[bool] = None,
    ttl: Optional[int] = None,
    paused: Optional[bool] = None,
    files: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Execute a Cloudflare remediation action (write operation)."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_cloudflare_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Cloudflare not connected. Please connect Cloudflare first."})

    action_type = action_type.lower().strip()
    handler = _ACTION_HANDLERS.get(action_type)
    if not handler:
        return json.dumps({
            "error": f"Invalid action_type '{action_type}'. Must be one of: {', '.join(sorted(VALID_ACTION_TYPES))}"
        })

    allowed = _get_enabled_zone_ids(user_id)
    if allowed is not None and zone_id not in allowed:
        return json.dumps({
            "error": f"Zone '{zone_id}' is not in your enabled zones. "
                     "Enable it on the Cloudflare settings page or use cloudflare_list_zones() to see available zones."
        })

    logger.info("[CLOUDFLARE-TOOL] user=%s action=%s zone=%s", user_id, action_type, zone_id)

    try:
        client = _build_client(creds)
        result = handler(client, zone_id,
                         value=value, record_id=record_id, rule_id=rule_id,
                         content=content, proxied=proxied, ttl=ttl,
                         paused=paused, files=files)

        if "error" in result:
            return json.dumps(result)

        result["success"] = True
        return json.dumps(result)

    except Exception as exc:
        import requests as _requests
        perm = _ACTION_PERMISSIONS.get(action_type, "unknown")
        if isinstance(exc, _requests.exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else "unknown"
            if status == 403:
                return json.dumps({
                    "error": f"Token lacks the '{perm}' permission required for {action_type}. "
                             f"Add the permission to the Cloudflare API token."
                })
            body = exc.response.text[:200] if exc.response is not None else ""
            return json.dumps({"error": f"Cloudflare API error ({status}): {body}"})
        if isinstance(exc, _requests.exceptions.Timeout):
            return json.dumps({"error": "Request timed out. Try again."})
        logger.error(f"[CLOUDFLARE-TOOL] Action {action_type} failed: {exc}")
        return json.dumps({"error": f"Failed to execute {action_type}: {str(exc)}"})
