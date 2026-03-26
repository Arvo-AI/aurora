"""
Cloudflare API client.

Provides an authenticated interface to the Cloudflare v4 API for use by
Aurora's route handlers and agent tools.  Covers both read-only diagnostics
and write/remediation actions (cache purge, security level, DNS, firewall).
"""

import logging
import math
import requests
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareAPIError(Exception):
    """Raised when Cloudflare returns success=false in a 200 response."""


class CloudflareClient:
    """Authenticated Cloudflare API client.

    Uses ``requests.Session`` for HTTP connection pooling across calls.
    """

    def __init__(self, api_token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, json_data: Optional[Dict] = None,
                  params: Optional[Dict] = None, timeout: int = 15) -> Dict[str, Any]:
        response = self._session.request(
            method,
            f"{CLOUDFLARE_API_BASE}{path}",
            json=json_data,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()

        # Cloudflare REST API returns {"success": false, "errors": [...]}
        # with HTTP 200 for API-level failures.  GraphQL uses a different
        # envelope so we skip the check for /graphql.
        if not path.startswith("/graphql") and data.get("success") is False:
            errors = data.get("errors") or []
            msg = "; ".join(
                e.get("message", f"error code {e.get('code', 'unknown')}")
                for e in errors
                if isinstance(e, dict)
            ) or "Unknown Cloudflare API error"
            logger.warning("[CF-API] %s %s returned success=false: %s", method, path, msg)
            raise CloudflareAPIError(msg)

        return data

    # -----------------------------------------------------------------
    # Account & token management
    # -----------------------------------------------------------------

    def list_accounts(self) -> List[Dict]:
        """Return accounts the token has access to (first page only).

        We only need the first result, hence why we only fetch one page.
        """
        data = self._request("GET", "/accounts", params={"per_page": 1})
        return data.get("result", [])

    def get_current_user(self) -> Dict:
        """Get the user associated with the current token."""
        data = self._request("GET", "/user")
        return data.get("result", {})

    def _extract_permission_names(self, policies: List[Dict]) -> List[str]:
        names: List[str] = []
        for policy in policies:
            if policy.get("effect") != "allow":
                continue
            for group in policy.get("permission_groups", []):
                name = group.get("name")
                if name:
                    names.append(name)
        return sorted(set(names))

    def get_token_permissions(self, token_id: str, account_id: Optional[str] = None) -> List[str]:
        """Fetch permission group names granted to this token.

        Tries the account-level endpoint first (for account-owned tokens),
        then falls back to the user-level endpoint (for user-owned tokens).
        """
        if account_id:
            try:
                data = self._request("GET", f"/accounts/{account_id}/tokens/{token_id}")
                return self._extract_permission_names(
                    data.get("result", {}).get("policies", []))
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (403, 404):
                    logger.info("Account token lookup failed (%s), falling back to user token endpoint", status)
                else:
                    raise
            except CloudflareAPIError:
                logger.info("Account token lookup returned API error, falling back to user token endpoint")

        try:
            data = self._request("GET", f"/user/tokens/{token_id}")
            return self._extract_permission_names(
                data.get("result", {}).get("policies", []))
        except Exception as e:
            logger.warning(f"Failed to fetch token permissions: {e}")
            return []

    # -----------------------------------------------------------------
    # Zones
    # -----------------------------------------------------------------

    def list_zones(self, account_id: Optional[str] = None) -> List[Dict]:
        """List all DNS zones, optionally filtered by account. Paginates automatically.

        Pagination is bounded by total_pages from Cloudflare's response, not
        an artificial cap.  Even large accounts rarely exceed a handful of pages
        (50 zones/page), and Cloudflare's rate limit is 1 200 req / 5 min. Hence, no upper cap on page count.
        """
        all_zones: List[Dict] = []
        page = 1

        while True:
            params: Dict[str, Any] = {"per_page": 50, "page": page}
            if account_id:
                params["account.id"] = account_id

            data = self._request("GET", "/zones", params=params)
            all_zones.extend(data.get("result", []))

            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_zones

    # -----------------------------------------------------------------
    # DNS records
    # -----------------------------------------------------------------

    def list_dns_records(self, zone_id: str, record_type: Optional[str] = None,
                         name: Optional[str] = None) -> List[Dict]:
        """List DNS records for a zone, optionally filtered by type or name. Paginates automatically."""
        all_records: List[Dict] = []
        page = 1

        while True:
            params: Dict[str, Any] = {"per_page": 100, "page": page}
            if record_type:
                params["type"] = record_type.upper()
            if name:
                params["name"] = name
            data = self._request("GET", f"/zones/{zone_id}/dns_records", params=params)
            all_records.extend(data.get("result", []))

            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_records

    # -----------------------------------------------------------------
    # Analytics
    # -----------------------------------------------------------------

    def get_zone_analytics(self, zone_id: str, since: str = "-1440",
                           until: Optional[str] = None,
                           limit: int = 1) -> List[Dict[str, Any]]:
        """Fetch zone analytics (requests, bandwidth, threats, status codes).

        Uses the Cloudflare GraphQL Analytics API.  The dataset node is chosen
        automatically based on the requested time window so that the ``limit``
        cap (max 100 rows) never silently truncates data:

        * Window <= 100 min  → ``httpRequests1mGroups`` (1-minute buckets)
        * Window <= 100 h    → ``httpRequests1hGroups`` (1-hour buckets)
        * Larger             → ``httpRequests1dGroups`` (1-day buckets)

        Args:
            since: Relative minutes as negative int string (e.g. ``"-1440"``
                   for last 24h) or ISO-8601 datetime.  Default is last 24h.
            until: Same format as ``since``.  Defaults to now.
            limit: Max number of time-bucket groups to return (1–100).  ``1``
                   returns a single bucket; higher values give a time-series.
                   When omitted the method auto-sizes to cover the full window.

        Returns:
            List of group dicts, each containing ``sum``, ``uniq``, and
            ``dimensions`` (with ``datetime`` or ``date`` key).
        """
        now = datetime.now(timezone.utc)
        start = self._parse_time(since, now, fallback_hours=24)
        end = self._parse_time(until, now) if until else now

        window_minutes = max(1, math.ceil((end - start).total_seconds() / 60))

        # Pick the coarsest bucket that covers the window within the 100-row cap.
        if window_minutes <= 100:
            dataset = "httpRequests1mGroups"
            filter_key_start = "datetime_geq"
            filter_key_end = "datetime_lt"
            dim_field = "datetime"
            auto_limit = window_minutes
        elif window_minutes <= 6000:  # <= 100 hours
            dataset = "httpRequests1hGroups"
            filter_key_start = "datetime_geq"
            filter_key_end = "datetime_lt"
            dim_field = "datetime"
            auto_limit = math.ceil(window_minutes / 60)
        else:
            dataset = "httpRequests1dGroups"
            filter_key_start = "date_geq"
            filter_key_end = "date_lt"
            dim_field = "date"
            auto_limit = math.ceil(window_minutes / 1440)

        query_limit = max(1, min(limit, 100))

        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        # date-only filters for daily dataset
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")

        filter_start = start_date if dim_field == "date" else start_str
        filter_end = end_date if dim_field == "date" else end_str

        query = f"""
        query ($zoneTag: String!, $start: {("Date" if dim_field == "date" else "Time")}!, $end: {("Date" if dim_field == "date" else "Time")}!, $limit: Int!) {{
          viewer {{
            zones(filter: {{zoneTag: $zoneTag}}) {{
              {dataset}(
                limit: $limit
                filter: {{{filter_key_start}: $start, {filter_key_end}: $end}}
                orderBy: [{dim_field}_ASC]
              ) {{
                dimensions {{
                  {dim_field}
                }}
                sum {{
                  bytes
                  cachedBytes
                  cachedRequests
                  encryptedBytes
                  encryptedRequests
                  requests
                  threats
                  pageViews
                  countryMap {{
                    bytes
                    requests
                    threats
                    clientCountryName
                  }}
                  responseStatusMap {{
                    requests
                    edgeResponseStatus
                  }}
                  threatPathingMap {{
                    requests
                    threatPathingName
                  }}
                  contentTypeMap {{
                    requests
                    bytes
                    edgeResponseContentTypeName
                  }}
                  clientHTTPVersionMap {{
                    requests
                    clientHTTPProtocol
                  }}
                  clientSSLMap {{
                    requests
                    clientSSLProtocol
                  }}
                  ipClassMap {{
                    requests
                    ipType
                  }}
                }}
                uniq {{
                  uniques
                }}
              }}
            }}
          }}
        }}
        """
        payload = {
            "query": query,
            "variables": {
                "zoneTag": zone_id,
                "start": filter_start,
                "end": filter_end,
                "limit": query_limit,
            },
        }
        data = self._request("POST", "/graphql", json_data=payload)

        errors = data.get("errors")
        if errors:
            msg = "; ".join(e.get("message", str(e)) for e in errors if isinstance(e, dict)) or str(errors)
            raise CloudflareAPIError(f"Analytics query failed: {msg}")

        try:
            viewer = (data.get("data") or {}).get("viewer") or {}
            zones = viewer.get("zones") or []
            if not zones:
                return []
            groups = zones[0].get(dataset) or []
            # Normalise the dimension key to "datetime" so callers don't have
            # to care which dataset was used.
            if dim_field != "datetime":
                for g in groups:
                    dims = g.get("dimensions") or {}
                    if dim_field in dims and "datetime" not in dims:
                        dims["datetime"] = dims.pop(dim_field)
            return groups
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            logger.warning("[CF-GRAPHQL] Failed to parse analytics response: %s", exc)
            return []

    @staticmethod
    def _parse_time(value: str, now, fallback_hours: int = 0):
        """Parse a time value: relative minutes (e.g. '-1440') or ISO-8601."""
        if not value:
            return now - timedelta(hours=fallback_hours) if fallback_hours else now
        if value.lstrip("-").isdigit():
            return now - timedelta(minutes=abs(int(value)))
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            return now - timedelta(hours=fallback_hours) if fallback_hours else now

    # -----------------------------------------------------------------
    # Firewall events (Security Events)
    # -----------------------------------------------------------------

    def get_firewall_events(self, zone_id: str, limit: int = 50,
                            since: Optional[str] = None,
                            until: Optional[str] = None) -> List[Dict]:
        """Fetch recent firewall / security events via the GraphQL Analytics API.

        Uses the ``firewallEventsAdaptive`` dataset which is available on all
        plan tiers.  Returns up to ``limit`` events ordered newest-first.
        """
        now = datetime.now(timezone.utc)
        start = self._parse_time(since, now, fallback_hours=24)
        end = self._parse_time(until, now) if until else now
        capped = min(limit, 100)

        query = """
        query ($zoneTag: String!, $since: Time!, $until: Time!, $limit: Int!) {
          viewer {
            zones(filter: {zoneTag: $zoneTag}) {
              firewallEventsAdaptive(
                filter: {datetime_gt: $since, datetime_lt: $until}
                limit: $limit
                orderBy: [datetime_DESC]
              ) {
                action
                clientIP
                clientRequestHTTPHost
                clientRequestPath
                clientRequestHTTPMethodName
                ruleId
                source
                userAgent
                datetime
              }
            }
          }
        }
        """
        payload = {
            "query": query,
            "variables": {
                "zoneTag": zone_id,
                "since": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "until": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": capped,
            },
        }
        data = self._request("POST", "/graphql", json_data=payload)

        errors = data.get("errors")
        if errors:
            msg = "; ".join(e.get("message", str(e)) for e in errors if isinstance(e, dict)) or str(errors)
            raise CloudflareAPIError(f"Firewall events query failed: {msg}")

        try:
            viewer = (data.get("data") or {}).get("viewer") or {}
            zones = viewer.get("zones") or []
            if zones:
                return zones[0].get("firewallEventsAdaptive") or []
        except (KeyError, IndexError, TypeError, AttributeError):
            logger.warning(
                "[CF-GRAPHQL] Failed to parse firewall events response; returning empty list",
                exc_info=True,
            )
        return []

    # -----------------------------------------------------------------
    # WAF / Firewall rules
    # -----------------------------------------------------------------

    def list_firewall_rules(self, zone_id: str) -> List[Dict]:
        """List all firewall rules for a zone. Paginates automatically."""
        all_rules: List[Dict] = []
        page = 1

        while True:
            params: Dict[str, Any] = {"per_page": 100, "page": page}
            data = self._request("GET", f"/zones/{zone_id}/firewall/rules", params=params)
            all_rules.extend(data.get("result", []))

            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_rules

    # -----------------------------------------------------------------
    # Rate Limiting
    # -----------------------------------------------------------------

    def list_rate_limits(self, zone_id: str) -> List[Dict]:
        """List rate limiting rules for a zone. Paginates automatically."""
        all_rules: List[Dict] = []
        page = 1

        while True:
            params: Dict[str, Any] = {"per_page": 100, "page": page}
            data = self._request("GET", f"/zones/{zone_id}/rate_limits", params=params)
            all_rules.extend(data.get("result", []))

            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_rules

    # -----------------------------------------------------------------
    # Workers
    # -----------------------------------------------------------------

    def list_workers(self, account_id: str) -> List[Dict]:
        """List Cloudflare Workers scripts for an account."""
        data = self._request("GET", f"/accounts/{account_id}/workers/scripts")
        return data.get("result", [])

    # -----------------------------------------------------------------
    # Load Balancers
    # -----------------------------------------------------------------

    def list_load_balancers(self, zone_id: str) -> List[Dict]:
        """List load balancers for a zone."""
        data = self._request("GET", f"/zones/{zone_id}/load_balancers")
        return data.get("result", [])

    # -----------------------------------------------------------------
    # SSL/TLS
    # -----------------------------------------------------------------

    def get_ssl_settings(self, zone_id: str) -> Dict[str, Any]:
        """Get the SSL/TLS mode for a zone (off, flexible, full, strict)."""
        data = self._request("GET", f"/zones/{zone_id}/settings/ssl")
        return data.get("result", {})

    def get_ssl_verification(self, zone_id: str) -> List[Dict]:
        """Get SSL certificate verification status for a zone."""
        data = self._request("GET", f"/zones/{zone_id}/ssl/verification")
        return data.get("result", [])

    # -----------------------------------------------------------------
    # Zone Settings (all settings in one call)
    # -----------------------------------------------------------------

    def get_zone_settings(self, zone_id: str) -> List[Dict]:
        """Fetch all settings for a zone (security_level, ssl, cache, dev mode, etc.)."""
        data = self._request("GET", f"/zones/{zone_id}/settings")
        return data.get("result", [])

    # -----------------------------------------------------------------
    # Page Rules
    # -----------------------------------------------------------------

    def list_page_rules(self, zone_id: str) -> List[Dict]:
        """List page rules for a zone (redirects, cache overrides, forwarding)."""
        data = self._request("GET", f"/zones/{zone_id}/pagerules",
                             params={"status": "active", "per_page": 50})
        return data.get("result", [])

    # -----------------------------------------------------------------
    # Cache / Purge (remediation)
    # -----------------------------------------------------------------

    def purge_cache(self, zone_id: str,
                    files: Optional[List[str]] = None) -> Dict[str, Any]:
        """Purge cache for a zone.  Either purge everything or specific file URLs."""
        payload: Dict[str, Any] = {}
        if files is not None:
            payload["files"] = files
        else:
            payload["purge_everything"] = True
        data = self._request("POST", f"/zones/{zone_id}/purge_cache", json_data=payload)
        return data.get("result", {})

    # -----------------------------------------------------------------
    # Security Level (remediation)
    # -----------------------------------------------------------------

    def set_security_level(self, zone_id: str, value: str) -> Dict[str, Any]:
        """Set zone security level. Valid values: essentially_off, low, medium, high, under_attack."""
        data = self._request("PATCH", f"/zones/{zone_id}/settings/security_level",
                             json_data={"value": value})
        return data.get("result", {})

    # -----------------------------------------------------------------
    # Development Mode (remediation)
    # -----------------------------------------------------------------

    def set_development_mode(self, zone_id: str, value: str) -> Dict[str, Any]:
        """Toggle development mode (bypasses cache). value: 'on' or 'off'."""
        data = self._request("PATCH", f"/zones/{zone_id}/settings/development_mode",
                             json_data={"value": value})
        return data.get("result", {})

    # -----------------------------------------------------------------
    # DNS Record Update (remediation)
    # -----------------------------------------------------------------

    def update_dns_record(self, zone_id: str, record_id: str,
                          content: Optional[str] = None,
                          proxied: Optional[bool] = None,
                          ttl: Optional[int] = None) -> Dict[str, Any]:
        """Patch a DNS record (change content/IP, proxied status, or TTL)."""
        payload: Dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if proxied is not None:
            payload["proxied"] = proxied
        if ttl is not None:
            payload["ttl"] = ttl
        if not payload:
            raise ValueError("At least one of content, proxied, or ttl must be provided")
        data = self._request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}",
                             json_data=payload)
        return data.get("result", {})

    # -----------------------------------------------------------------
    # Firewall Rule Toggle (remediation)
    # -----------------------------------------------------------------

    def update_firewall_rule_paused(self, zone_id: str, rule_id: str,
                                    paused: bool) -> Dict[str, Any]:
        """Enable or disable a firewall rule by setting its paused state."""
        data = self._request(
            "PATCH",
            f"/zones/{zone_id}/firewall/rules/{rule_id}",
            json_data={"paused": paused},
        )
        return data.get("result", {})

    # -----------------------------------------------------------------
    # Healthchecks
    # -----------------------------------------------------------------

    def list_healthchecks(self, zone_id: str) -> List[Dict]:
        """List configured healthchecks for a zone."""
        data = self._request("GET", f"/zones/{zone_id}/healthchecks")
        return data.get("result", [])
