"""
Sentry web API client (Internal Integration auth token).

Wraps the read-only endpoints Aurora uses for incident RCA:
- Organization + project discovery
- Issue search and detail (with full event/stacktrace)
- Discover-style event search

Auth model: Internal Integration auth tokens. The token is bearer-style and
carries the integration's permissions (configured in Sentry by the customer
during integration creation). Tokens do not expire until revoked.

See: https://docs.sentry.io/api/ and
https://docs.sentry.io/integrations/integration-platform/internal-integration/
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

import requests

QueryParams = Union[Dict[str, Any], Sequence[Tuple[str, Any]]]

logger = logging.getLogger(__name__)

SENTRY_REGION_HOSTS = {
    "us": "https://sentry.io",
    "eu": "https://de.sentry.io",
}
DEFAULT_REGION = "us"

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0

# Sentry uses cursor-based pagination via the Link header.
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="(\w+)";\s*results="(\w+)"(?:;\s*cursor="([^"]*)")?')


class SentryAPIError(Exception):
    """Raised when the Sentry API returns a non-success response."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[Any] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SentryClient:
    """Read-only client for the Sentry web API.

    Constructed per-invocation with credentials from Vault — never reads from
    process state or shared globals.
    """

    def __init__(
        self,
        auth_token: str,
        org_slug: str,
        region: str = DEFAULT_REGION,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not auth_token or not isinstance(auth_token, str):
            raise ValueError("Sentry auth token is required")
        if not org_slug or not isinstance(org_slug, str):
            raise ValueError("Sentry organization slug is required")

        normalized_region = (region or DEFAULT_REGION).lower().strip()
        if normalized_region not in SENTRY_REGION_HOSTS:
            raise ValueError(f"Unsupported Sentry region '{region}'. Use one of: {list(SENTRY_REGION_HOSTS)}")

        self.auth_token = auth_token
        self.org_slug = org_slug.strip()
        self.region = normalized_region
        self.base_url = SENTRY_REGION_HOSTS[normalized_region]
        self.timeout = timeout

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[QueryParams] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        """Issue a Sentry API request with retry/backoff. Returns (json_body, response_headers)."""
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"

        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )

                if response.status_code == 401:
                    raise SentryAPIError("Invalid Sentry auth token", status_code=401)
                if response.status_code == 403:
                    raise SentryAPIError("Sentry auth token lacks required permissions", status_code=403)
                if response.status_code == 404:
                    raise SentryAPIError("Sentry resource not found", status_code=404)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "[SENTRY] Rate limited (429), retrying in %ds (attempt %d/%d)",
                            retry_after, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(min(retry_after, 30))
                        continue
                    raise SentryAPIError("Sentry rate limit exceeded", status_code=429)

                if response.status_code >= 500:
                    if attempt < MAX_RETRIES:
                        wait = RETRY_BACKOFF * (2 ** attempt)
                        logger.warning(
                            "[SENTRY] %s server error, retrying in %.1fs (attempt %d/%d)",
                            response.status_code, wait, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    raise SentryAPIError(
                        f"Sentry server error {response.status_code}",
                        status_code=response.status_code,
                    )

                response.raise_for_status()

                if response.status_code == 204 or not response.content:
                    return None, dict(response.headers)
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                return body, dict(response.headers)

            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "[SENTRY] Network error, retrying in %.1fs (attempt %d/%d): %s",
                        wait, attempt + 1, MAX_RETRIES, type(exc).__name__,
                    )
                    time.sleep(wait)
                    continue
                break
            except SentryAPIError:
                raise
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                try:
                    err_body = exc.response.json()
                except Exception:  # noqa: BLE001
                    err_body = exc.response.text if exc.response is not None else None
                raise SentryAPIError(
                    f"Sentry HTTP error {status}", status_code=status, body=err_body,
                ) from exc

        raise SentryAPIError(
            f"Sentry request failed after {MAX_RETRIES + 1} attempts: {last_error}",
        )

    @staticmethod
    def _parse_next_cursor(link_header: str) -> Optional[str]:
        """Extract the next-page cursor from a Sentry Link header, if any."""
        if not link_header:
            return None
        for url, rel, results, cursor in _LINK_RE.findall(link_header):
            if rel == "next" and results == "true":
                if cursor:
                    return cursor
                parsed = urlparse(url)
                for chunk in parsed.query.split("&"):
                    if chunk.startswith("cursor="):
                        return chunk[len("cursor="):]
        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_credentials(self) -> Dict[str, Any]:
        """Validate the token against the configured org. Returns org metadata."""
        body, _ = self._request("GET", f"/api/0/organizations/{self.org_slug}/")
        if not isinstance(body, dict) or not body.get("slug"):
            raise SentryAPIError("Sentry organization lookup returned no data")
        return body

    def list_accessible_organizations(self) -> List[Dict[str, Any]]:
        """List orgs the auth token can see. For Internal Integrations this is
        a single-element list (the integration's owning org)."""
        body, _ = self._request("GET", "/api/0/organizations/")
        if not isinstance(body, list):
            return []
        return body

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List projects in the configured org."""
        body, _ = self._request(
            "GET",
            f"/api/0/organizations/{self.org_slug}/projects/",
            params={"per_page": min(limit, 100)},
        )
        if not isinstance(body, list):
            return []
        return body

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def list_issues(
        self,
        query: str = "is:unresolved",
        stats_period: str = "24h",
        project: Optional[List[str]] = None,
        environment: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None,
        sort: str = "date",
    ) -> Dict[str, Any]:
        """Search issues. Returns {issues, next_cursor}.

        See https://docs.sentry.io/api/events/list-an-organizations-issues/
        """
        params: Dict[str, Any] = {
            "query": query,
            "statsPeriod": stats_period,
            "sort": sort,
            "limit": min(max(limit, 1), 100),
        }
        if project:
            params["project"] = project
        if environment:
            params["environment"] = environment
        if cursor:
            params["cursor"] = cursor

        body, headers = self._request(
            "GET",
            f"/api/0/organizations/{self.org_slug}/issues/",
            params=params,
        )
        issues = body if isinstance(body, list) else []
        return {
            "issues": issues,
            "count": len(issues),
            "next_cursor": self._parse_next_cursor(headers.get("Link", "")),
        }

    def get_issue(self, issue_id: str) -> Dict[str, Any]:
        """Fetch issue metadata."""
        body, _ = self._request(
            "GET",
            f"/api/0/organizations/{self.org_slug}/issues/{issue_id}/",
        )
        if not isinstance(body, dict):
            raise SentryAPIError("Unexpected Sentry issue payload")
        return body

    def get_issue_latest_event(self, issue_id: str) -> Dict[str, Any]:
        """Fetch the latest event for an issue (includes stacktrace, breadcrumbs, tags)."""
        body, _ = self._request(
            "GET",
            f"/api/0/organizations/{self.org_slug}/issues/{issue_id}/events/latest/",
        )
        if not isinstance(body, dict):
            raise SentryAPIError("Unexpected Sentry event payload")
        return body

    # ------------------------------------------------------------------
    # Discover event search
    # ------------------------------------------------------------------

    def search_events(
        self,
        query: str,
        stats_period: str = "24h",
        fields: Optional[List[str]] = None,
        sort: str = "-timestamp",
        per_page: int = 50,
        project: Optional[List[str]] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Discover-style event search (table view).

        See https://docs.sentry.io/api/discover/query-discover-events-in-table-format
        """
        default_fields = [
            "id",
            "title",
            "project",
            "timestamp",
            "level",
            "culprit",
            "user.email",
            "environment",
        ]
        params: List[Tuple[str, Any]] = [
            ("query", query or ""),
            ("statsPeriod", stats_period),
            ("sort", sort),
            ("per_page", min(max(per_page, 1), 100)),
        ]
        for f in (fields or default_fields):
            params.append(("field", f))
        if project:
            for p in project:
                params.append(("project", p))
        if environment:
            params.append(("environment", environment))

        body, _ = self._request(
            "GET",
            f"/api/0/organizations/{self.org_slug}/events/",
            params=params,
        )
        rows = (body or {}).get("data", []) if isinstance(body, dict) else []
        meta = (body or {}).get("meta", {}) if isinstance(body, dict) else {}
        return {"events": rows, "meta": meta, "count": len(rows)}
