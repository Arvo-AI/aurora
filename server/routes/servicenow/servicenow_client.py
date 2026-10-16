"""ServiceNow Table API client shared by the connector routes, the webhook
pipeline, the RCA work-note post-back and the agent tools.

Auth is either basic (a dedicated integration user) or OAuth 2.0 client
credentials (POST /oauth_token.do; the bearer token is cached per instance
and client until shortly before it expires). Only https hosts on an allowed
ServiceNow domain are accepted, so stored credentials can never be pointed at
an arbitrary URL.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

SERVICENOW_TIMEOUT = 20
INCIDENT_TABLE_PATH = "/api/now/table/incident"
# Fields returned to the agent tools and used by the connection check.
INCIDENT_FIELDS = (
    "sys_id,number,short_description,description,priority,impact,urgency,state,"
    "category,subcategory,assignment_group,assigned_to,cmdb_ci,caller_id,"
    "opened_at,resolved_at,closed_at,close_notes,sys_updated_on"
)

_DEFAULT_ALLOWED_SUFFIXES = (".service-now.com", ".servicenowservices.com")
_SYS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_NUMBER_RE = re.compile(r"^[A-Za-z]{2,10}\d{4,12}$")
# Refresh a cached OAuth token this many seconds before ServiceNow expires it.
_TOKEN_REFRESH_MARGIN = 60
_token_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
_token_lock = threading.Lock()


class ServiceNowAPIError(Exception):
    """Vendor error carrying the HTTP status when one came back (None = no response)."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def is_sys_id(value: Any) -> bool:
    return bool(value) and bool(_SYS_ID_RE.fullmatch(str(value)))


def is_incident_number(value: Any) -> bool:
    return bool(value) and bool(_NUMBER_RE.fullmatch(str(value)))


def allowed_host_suffixes() -> Tuple[str, ...]:
    """Host suffixes accepted for an instance URL.

    SERVICENOW_ALLOWED_HOSTS (comma-separated) adds suffixes for customers on
    a custom domain; each entry is matched as a dotted suffix.
    """
    suffixes = list(_DEFAULT_ALLOWED_SUFFIXES)
    for item in (os.getenv("SERVICENOW_ALLOWED_HOSTS") or "").split(","):
        item = item.strip().lower()
        if item:
            suffixes.append(item if item.startswith(".") else "." + item)
    return tuple(suffixes)


def normalize_instance_url(raw: str) -> str:
    """Return ``https://<host>`` for a valid ServiceNow instance URL; raise ValueError otherwise."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("Instance URL is required")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("Instance URL must use https")
    host = (parsed.hostname or "").lower()
    if not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Instance URL must be a plain https host")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("Instance URL must not include a path or query string")
    if not any(host.endswith(suffix) for suffix in allowed_host_suffixes()):
        raise ValueError(
            "Instance URL must be a ServiceNow host (for example https://acme.service-now.com)"
        )
    return f"https://{host}"


def incident_url(instance_url: str, sys_id: str) -> str:
    """Deep link to an incident record (classic navigation URL, valid on every release)."""
    return f"{instance_url}/nav_to.do?uri=incident.do?sys_id={sys_id}"


class ServiceNowClient:
    """Thin Table API client. Every method raises ServiceNowAPIError on failure."""

    def __init__(
        self,
        instance_url: str,
        auth_type: str = "basic",
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        timeout: int = SERVICENOW_TIMEOUT,
    ):
        self.instance_url = normalize_instance_url(instance_url)
        self.auth_type = auth_type if auth_type == "oauth_client_credentials" else "basic"
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    @classmethod
    def from_credentials(cls, creds: Dict[str, Any]) -> "ServiceNowClient":
        return cls(
            instance_url=creds.get("instance_url") or "",
            auth_type=creds.get("auth_type") or "basic",
            username=creds.get("username"),
            password=creds.get("password"),
            client_id=creds.get("client_id"),
            client_secret=creds.get("client_secret"),
        )

    # ── auth ─────────────────────────────────────────────────────────

    def _cache_key(self) -> Tuple[str, str]:
        return (self.instance_url, self.client_id or "")

    def _oauth_token(self) -> str:
        now = time.monotonic()
        with _token_lock:
            cached = _token_cache.get(self._cache_key())
            if cached and cached[1] > now:
                return cached[0]
        try:
            resp = requests.post(
                f"{self.instance_url}/oauth_token.do",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id or "",
                    "client_secret": self.client_secret or "",
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise ServiceNowAPIError("Request to ServiceNow timed out") from exc
        except requests.RequestException as exc:
            raise ServiceNowAPIError(
                f"Unable to reach ServiceNow for an OAuth token: {exc.__class__.__name__}"
            ) from exc
        if resp.status_code != 200:
            raise ServiceNowAPIError(
                "ServiceNow rejected the OAuth client credentials", resp.status_code
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ServiceNowAPIError(
                "ServiceNow returned a non-JSON OAuth response", resp.status_code
            ) from exc
        token = data.get("access_token")
        if not token:
            raise ServiceNowAPIError("ServiceNow OAuth response had no access_token", resp.status_code)
        try:
            ttl = float(data.get("expires_in") or 1800)
        except (TypeError, ValueError):
            ttl = 1800.0
        with _token_lock:
            _token_cache[self._cache_key()] = (token, now + max(ttl - _TOKEN_REFRESH_MARGIN, 30))
        return token

    def _forget_token(self) -> None:
        with _token_lock:
            _token_cache.pop(self._cache_key(), None)

    # ── transport ────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = None
        if self.auth_type == "oauth_client_credentials":
            headers["Authorization"] = f"Bearer {self._oauth_token()}"
        else:
            auth = (self.username or "", self.password or "")
        url = f"{self.instance_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=json_body,
                auth=auth, timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise ServiceNowAPIError("Request to ServiceNow timed out") from exc
        except requests.RequestException as exc:
            raise ServiceNowAPIError(f"Unable to reach ServiceNow: {exc.__class__.__name__}") from exc

        status = resp.status_code
        if status == 401:
            if self.auth_type == "oauth_client_credentials":
                self._forget_token()
            raise ServiceNowAPIError("Authentication failed: check the ServiceNow credentials", 401)
        if status == 403:
            raise ServiceNowAPIError(
                "ServiceNow refused the request: the account lacks the required role or ACL", 403
            )
        if status == 404:
            raise ServiceNowAPIError("ServiceNow record or endpoint not found", 404)
        if status >= 400:
            raise ServiceNowAPIError(f"ServiceNow API error (HTTP {status})", status)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise ServiceNowAPIError("ServiceNow returned a non-JSON response", status) from exc

    # ── operations ───────────────────────────────────────────────────

    def test_connection(self) -> Dict[str, Any]:
        """Read one incident: proves the host, the credentials and incident read access."""
        data = self._request(
            "GET", INCIDENT_TABLE_PATH,
            params={"sysparm_limit": 1, "sysparm_fields": "sys_id,number"},
        )
        result = data.get("result")
        if not isinstance(result, list):
            raise ServiceNowAPIError("Unexpected response from the ServiceNow Table API")
        return {"ok": True, "sample_count": len(result)}

    def list_incidents(
        self, query: Optional[str] = None, limit: int = 25, fields: str = INCIDENT_FIELDS,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "sysparm_limit": max(1, min(int(limit), 100)),
            "sysparm_fields": fields,
            "sysparm_display_value": "true",
            "sysparm_exclude_reference_link": "true",
        }
        if query:
            params["sysparm_query"] = query
        data = self._request("GET", INCIDENT_TABLE_PATH, params=params)
        result = data.get("result")
        return result if isinstance(result, list) else []

    def get_incident(self, ref: str, fields: str = INCIDENT_FIELDS) -> Optional[Dict[str, Any]]:
        """Fetch one incident by sys_id or by number (INC0010001). None when not found."""
        ref = (ref or "").strip()
        if is_sys_id(ref):
            try:
                data = self._request(
                    "GET", f"{INCIDENT_TABLE_PATH}/{ref}",
                    params={
                        "sysparm_fields": fields,
                        "sysparm_display_value": "true",
                        "sysparm_exclude_reference_link": "true",
                    },
                )
            except ServiceNowAPIError as exc:
                if exc.status_code == 404:
                    return None
                raise
            result = data.get("result")
            return result if isinstance(result, dict) else None
        if is_incident_number(ref):
            rows = self.list_incidents(query=f"number={ref}", limit=1, fields=fields)
            return rows[0] if rows else None
        raise ValueError("Incident reference must be a sys_id or an incident number such as INC0010001")

    def add_work_note(self, sys_id: str, text: str) -> Dict[str, Any]:
        """Append one work note to an incident. Returns the updated record fields."""
        if not is_sys_id(sys_id):
            raise ValueError("sys_id must be a 32-character hex id")
        data = self._request(
            "PATCH", f"{INCIDENT_TABLE_PATH}/{sys_id}",
            params={"sysparm_fields": "sys_id,number,sys_updated_on"},
            json_body={"work_notes": text},
        )
        result = data.get("result")
        return result if isinstance(result, dict) else {}
