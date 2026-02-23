"""
Coroot API client with session-cookie authentication.

Uses email/password login to obtain a `coroot_session` cookie (7-day TTL)
and transparently re-authenticates on 401 responses.
"""

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

COROOT_TIMEOUT = 30


class CorootAPIError(Exception):
    """Custom error for Coroot API interactions."""


class CorootClient:
    """HTTP client for a standard Coroot installation.

    All data is accessed through Coroot's HTTP API using session-cookie auth.
    No direct ClickHouse or Prometheus connections are required.
    """

    def __init__(
        self,
        url: str,
        email: str,
        password: str,
        session_cookie: Optional[str] = None,
    ):
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.session_cookie = session_cookie
        self._session = requests.Session()
        if session_cookie:
            self._session.cookies.set("coroot_session", session_cookie)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Authenticate and store the session cookie.

        Returns the ``coroot_session`` cookie value.
        """
        try:
            resp = self._session.post(
                f"{self.url}/api/login",
                json={"email": self.email, "password": self.password},
                timeout=COROOT_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("[COROOT] Login network error: %s", exc)
            raise CorootAPIError("Unable to reach Coroot server") from exc

        if resp.status_code in (401, 404):
            raise CorootAPIError("Invalid email or password")
        if not resp.ok:
            raise CorootAPIError(f"Login failed (HTTP {resp.status_code})")

        cookie = self._session.cookies.get("coroot_session")
        if not cookie:
            raise CorootAPIError(
                "Login succeeded but no session cookie was returned"
            )

        self.session_cookie = cookie
        logger.info("[COROOT] Login successful")
        return cookie

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        """Send an authenticated request, re-logging in on 401."""
        if not self.session_cookie:
            self.login()

        kwargs.setdefault("timeout", COROOT_TIMEOUT)
        url = f"{self.url}{path}"

        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            logger.error("[COROOT] %s %s network error: %s", method, path, exc)
            raise CorootAPIError("Unable to reach Coroot server") from exc

        if resp.status_code == 401:
            logger.info("[COROOT] Session expired, re-authenticating")
            self.login()
            try:
                resp = self._session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                raise CorootAPIError("Unable to reach Coroot server") from exc

        if resp.status_code == 404:
            raise CorootAPIError(f"Resource not found: {path}")

        if not resp.ok:
            logger.error(
                "[COROOT] %s %s failed (%s): %s",
                method, path, resp.status_code, resp.text[:500],
            )
            raise CorootAPIError(
                resp.text[:200] or f"Request failed (HTTP {resp.status_code})"
            )

        return resp.json()

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    # ------------------------------------------------------------------
    # Envelope helpers
    # ------------------------------------------------------------------

    def _unwrap(self, response: Any) -> Any:
        """Extract ``.data`` from the standard Coroot response envelope."""
        if isinstance(response, dict) and "data" in response:
            return response["data"]
        return response

    @staticmethod
    def _time_params(from_ts: int, to_ts: int) -> Dict[str, str]:
        return {"from": str(from_ts), "to": str(to_ts)}

    @staticmethod
    def _encode_app_id(app_id: str) -> str:
        return quote(app_id, safe="")

    # ------------------------------------------------------------------
    # Project discovery
    # ------------------------------------------------------------------

    def discover_projects(self) -> List[Dict[str, Any]]:
        """Return the list of Coroot projects."""
        data = self._get("/api/project/")
        if isinstance(data, list):
            return data
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Application health & audit reports
    # ------------------------------------------------------------------

    def get_applications(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/applications",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    def get_app_detail(
        self, project: str, app_id: str, from_ts: int, to_ts: int
    ) -> Any:
        encoded = self._encode_app_id(app_id)
        resp = self._get(
            f"/api/project/{project}/app/{encoded}",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def get_app_logs(
        self,
        project: str,
        app_id: str,
        from_ts: int,
        to_ts: int,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        encoded = self._encode_app_id(app_id)
        params: Dict[str, str] = self._time_params(from_ts, to_ts)
        if query:
            params["query"] = json.dumps(query)
        resp = self._get(
            f"/api/project/{project}/app/{encoded}/logs",
            params=params,
        )
        return self._unwrap(resp)

    def get_overview_logs(
        self,
        project: str,
        from_ts: int,
        to_ts: int,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params: Dict[str, str] = self._time_params(from_ts, to_ts)
        if query:
            params["query"] = json.dumps(query)
        resp = self._get(
            f"/api/project/{project}/overview/logs",
            params=params,
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------

    def get_traces(
        self,
        project: str,
        from_ts: int,
        to_ts: int,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params: Dict[str, str] = self._time_params(from_ts, to_ts)
        if query:
            params["query"] = json.dumps(query)
        resp = self._get(
            f"/api/project/{project}/overview/traces",
            params=params,
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Incidents & RCA
    # ------------------------------------------------------------------

    def get_incidents(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/incidents",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    def get_incident_detail(
        self, project: str, incident_key: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/incident/{incident_key}",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Service dependency map
    # ------------------------------------------------------------------

    def get_service_map(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/map",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def get_nodes(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/nodes",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    def get_node_detail(
        self, project: str, node: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/node/{quote(node, safe='')}",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Metrics (PromQL)
    # ------------------------------------------------------------------

    def query_prom(
        self,
        project: str,
        query: str,
        start: int,
        end: int,
        step: str = "60s",
    ) -> Any:
        """Prometheus-compatible query_range. Returns raw Prom JSON (no envelope)."""
        return self._get(
            f"/api/project/{project}/prom/api/v1/query_range",
            params={
                "query": query,
                "start": str(start),
                "end": str(end),
                "step": step,
            },
        )

    # ------------------------------------------------------------------
    # Deployments, costs, risks
    # ------------------------------------------------------------------

    def get_deployments(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/deployments",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    def get_costs(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/costs",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    def get_risks(
        self, project: str, from_ts: int, to_ts: int
    ) -> Any:
        resp = self._get(
            f"/api/project/{project}/overview/risks",
            params=self._time_params(from_ts, to_ts),
        )
        return self._unwrap(resp)

    # ------------------------------------------------------------------
    # Future improvements (Coroot API capabilities not yet wired to tools):
    # - Profiling: GET /app/{app}/profiling returns flamegraph call trees.
    #   A summarizer extracting top-N CPU hotspots (highest `self` time)
    #   and the hot path from root to leaf would make this actionable.
    # - Per-app traces: GET /app/{app}/tracing scopes traces to one app.
    #   Redundant today (cross-app endpoint + ServiceName filter), but
    #   could improve performance on large clusters.
    # - Metric discovery: GET /prom/api/v1/series and /label/{name}/values
    #   help the agent explore unknown environments where metric names
    #   and label values aren't known upfront.
    # ------------------------------------------------------------------
