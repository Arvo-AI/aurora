"""Grafana Loki HTTP API client.

Thin wrapper around Loki's v1 HTTP API using ``requests.Session`` for
connection pooling.  Supports bearer-token, basic-auth, and
unauthenticated access with optional multi-tenant ``X-Scope-OrgID``.

Follows the BigPanda / Coroot client pattern established in Aurora:
custom exception class, ``_request`` helper, per-endpoint methods.
"""

import logging
from typing import Any, Dict, List, Optional, Union

import requests

logger = logging.getLogger("loki_client")

LOKI_TIMEOUT = 30


class LokiAPIError(Exception):
    """Custom error for Loki API interactions."""


class LokiClient:
    """HTTP client for Grafana Loki API v1.

    Supports bearer token, basic auth, and unauthenticated access.
    Optional ``X-Scope-OrgID`` header for multi-tenant deployments.
    """

    def __init__(
        self,
        base_url: str,
        auth_type: str = "none",
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        self.tenant_id = tenant_id
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

        if auth_type == "bearer" and token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "basic" and username and password:
            self._session.auth = (username, password)

        if tenant_id:
            self._session.headers["X-Scope-OrgID"] = tenant_id

    # ------------------------------------------------------------------
    # Internal request helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Send a request and return the parsed JSON response."""
        kwargs.setdefault("timeout", LOKI_TIMEOUT)
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.error("[LOKI] %s %s failed: %s", method, path, exc)
            raise LokiAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error("[LOKI] %s %s error: %s", method, path, exc)
            raise LokiAPIError("Unable to reach Loki") from exc

    def _request_raw(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send a request and return the raw :class:`requests.Response`.

        Used for endpoints that do not return JSON (e.g. ``/ready``).
        """
        kwargs.setdefault("timeout", LOKI_TIMEOUT)
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            logger.error("[LOKI] %s %s failed: %s", method, path, exc)
            raise LokiAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error("[LOKI] %s %s error: %s", method, path, exc)
            raise LokiAPIError("Unable to reach Loki") from exc

    # ------------------------------------------------------------------
    # Connection validation
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Two-step connection check: readiness probe then credential validation.

        1. ``GET /ready`` -- verifies the Loki instance is reachable and ready.
        2. ``GET /loki/api/v1/labels`` -- verifies credentials have read access.

        Returns ``{"ready": True, "labels": [...]}``.
        """
        # Step 1: readiness probe (plain-text response, not JSON)
        resp = self._request_raw("GET", "/ready")
        if "ready" not in resp.text.lower():
            raise LokiAPIError("Loki instance is not ready")

        # Step 2: credential validation via labels endpoint
        data = self._request("GET", "/loki/api/v1/labels")
        return {"ready": True, "labels": data.get("data", [])}

    # ------------------------------------------------------------------
    # Query endpoints
    # ------------------------------------------------------------------

    def query_range(
        self,
        query: str,
        start: str,
        end: str,
        limit: int = 100,
        direction: str = "backward",
        step: Optional[str] = None,
    ) -> dict:
        """Execute a range query over a time window."""
        params: Dict[str, Any] = {
            "query": query,
            "start": start,
            "end": end,
            "limit": limit,
            "direction": direction,
        }
        if step:
            params["step"] = step
        return self._request("GET", "/loki/api/v1/query_range", params=params)

    def query(
        self,
        query: str,
        limit: int = 100,
        time: Optional[str] = None,
        direction: str = "backward",
    ) -> dict:
        """Execute an instant query at a single point in time."""
        params: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "direction": direction,
        }
        if time:
            params["time"] = time
        return self._request("GET", "/loki/api/v1/query", params=params)

    # ------------------------------------------------------------------
    # Label / series discovery
    # ------------------------------------------------------------------

    def labels(self, start: Optional[str] = None, end: Optional[str] = None) -> list:
        """List all known label names."""
        params: Dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/loki/api/v1/labels", params=params)
        return data.get("data", [])

    def label_values(
        self,
        label: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> list:
        """List known values for a specific label."""
        params: Dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request(
            "GET", f"/loki/api/v1/label/{label}/values", params=params
        )
        return data.get("data", [])

    def series(
        self,
        match: Union[str, List[str]],
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> list:
        """List unique stream label sets matching the given selector(s)."""
        params: Dict[str, Any] = {"match[]": match}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        data = self._request("GET", "/loki/api/v1/series", params=params)
        return data.get("data", [])

    # ------------------------------------------------------------------
    # Statistics and rules
    # ------------------------------------------------------------------

    def index_stats(self, query: str, start: str, end: str) -> dict:
        """Retrieve index statistics for a query over a time range."""
        return self._request(
            "GET",
            "/loki/api/v1/index/stats",
            params={"query": query, "start": start, "end": end},
        )

    def get_rules(self) -> dict:
        """Retrieve all configured alerting and recording rules."""
        return self._request("GET", "/loki/api/v1/rules")

    def get_alerts(self) -> dict:
        """Retrieve currently firing alerts from the Loki ruler."""
        return self._request("GET", "/prometheus/api/v1/alerts")
