"""
Coroot API client with session-cookie authentication.

Uses email/password login to obtain a `coroot_session` cookie (7-day TTL)
and transparently re-authenticates on 401 responses.

Use :func:`get_coroot_client` to obtain a cached, authenticated client
instance for a given user.  The cache keeps a single ``CorootClient`` per
user_id alive for ``CLIENT_CACHE_TTL`` seconds so that the underlying
``requests.Session`` (and its session cookie) are reused across Flask
requests and agent tool calls.
"""

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

COROOT_TIMEOUT = 30
CLIENT_CACHE_TTL = 36000  # 10 hours — within the 7-day cookie TTL

_client_cache: Dict[str, Tuple["CorootClient", float]] = {}
_cache_lock = threading.Lock()
_user_locks: Dict[str, threading.Lock] = {}
_last_sweep: float = 0.0
_SWEEP_INTERVAL: float = CLIENT_CACHE_TTL / 2


class CorootAPIError(Exception):
    """Custom error for Coroot API interactions."""


def _get_user_lock(user_id: str) -> threading.Lock:
    with _cache_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def _sweep_stale_locks() -> None:
    """Remove ``_user_locks`` entries whose user_id has no live cache entry.

    Must be called while holding ``_cache_lock``.
    """
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    stale = [uid for uid in _user_locks if uid not in _client_cache]
    for uid in stale:
        del _user_locks[uid]
    if stale:
        logger.debug("[COROOT] Swept %d stale user locks", len(stale))


def get_coroot_client(
    user_id: str,
    url: str,
    email: str,
    password: str,
) -> "CorootClient":
    """Return a cached, authenticated :class:`CorootClient` for *user_id*.

    A new client is created (and logged in) when no cached entry exists,
    the TTL has expired, or the credentials have changed.

    A per-user lock ensures that concurrent requests for the same user
    don't race through login() simultaneously.
    """
    user_lock = _get_user_lock(user_id)
    with user_lock:
        now = time.monotonic()
        with _cache_lock:
            entry = _client_cache.get(user_id)
        if entry is not None:
            client, created_at = entry
            creds_match = (
                client.url == url.rstrip("/")
                and client.email == email
                and client.password == password
            )
            if creds_match and (now - created_at) < CLIENT_CACHE_TTL:
                return client

        client = CorootClient(url=url, email=email, password=password)
        client.login()

        with _cache_lock:
            if entry is not None:
                previous_client = entry[0]
                try:
                    previous_client._session.close()
                except Exception:
                    logger.debug(
                        "[COROOT] Failed to close session for user_id=%s (replacement)",
                        user_id,
                        exc_info=True,
                    )
            _client_cache[user_id] = (client, now)
            _sweep_stale_locks()
        return client


def invalidate_coroot_client(user_id: str) -> None:
    """Remove *user_id*'s client from the cache (e.g. on disconnect)."""
    with _cache_lock:
        entry = _client_cache.pop(user_id, None)
        _user_locks.pop(user_id, None)
    if entry is not None:
        client = entry[0]
        try:
            client._session.close()
        except Exception:
            # Best-effort cleanup: ignore errors when closing the session.
            logger.debug(
                "[COROOT] Failed to close session for user_id=%s",
                user_id,
                exc_info=True,
            )


class CorootClient:
    """HTTP client for a standard Coroot installation.

    All data is accessed through Coroot's HTTP API using session-cookie auth.
    No direct ClickHouse or Prometheus connections are required.

    Prefer :func:`get_coroot_client` over constructing this directly.
    """

    def __init__(self, url: str, email: str, password: str):
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.session_cookie: Optional[str] = None
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Authenticate and store the session cookie.

        Returns the ``coroot_session`` cookie value.
        """
        self._session.cookies.clear()

        try:
            resp = self._session.post(
                f"{self.url}/api/login",
                json={"email": self.email, "password": self.password},
                timeout=COROOT_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("[COROOT] Login network error: %s", exc)
            raise CorootAPIError("Unable to reach Coroot server") from exc

        if resp.status_code == 401:
            raise CorootAPIError(
                f"Invalid email or password"
            )
        if resp.status_code == 404:
            raise CorootAPIError(
                f"Login endpoint not found — check base URL"
            )
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
                "[COROOT] %s %s failed (HTTP %s)",
                method, path, resp.status_code,
            )
            logger.debug(
                "Coroot response body: %s",
                (resp.text[:1000] if resp.text else "(empty)"),
            )
            raise CorootAPIError(
                f"Request to Coroot failed (HTTP {resp.status_code})"
            )

        try:
            return resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise CorootAPIError(f"Non-JSON response from {path}")

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
        if from_ts < 1e12:
            from_ts *= 1000
        if to_ts < 1e12:
            to_ts *= 1000
        return {"from": str(from_ts), "to": str(to_ts)}

    @staticmethod
    def _encode_app_id(app_id: str) -> str:
        return quote(app_id, safe="")

    # ------------------------------------------------------------------
    # Project discovery
    # ------------------------------------------------------------------

    def discover_projects(self) -> List[Dict[str, Any]]:
        """Return the list of Coroot projects with their internal IDs.

        Coroot's ``GET /api/user`` returns a ``projects`` list with both
        the opaque hash ``id`` (needed for all data endpoints) and the
        human-readable ``name``.
        """
        data = self._get("/api/user")
        if isinstance(data, dict):
            projects = data.get("projects")
            if isinstance(projects, list):
                return projects
        return []

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
    # Metrics (PromQL via panel/data)
    # ------------------------------------------------------------------

    def query_panel_data(
        self,
        project: str,
        promql: str,
        from_ts: int,
        to_ts: int,
        legend: str = "",
    ) -> Any:
        """Execute a PromQL query via Coroot's dashboard panel/data endpoint.

        Returns ``{"chart": {"ctx": {...}, "series": [...]}}`` where each
        series has ``name`` (label string) and ``data`` (list of values).

        Caveat: panel config is sent as a query param; long PromQL may
        exceed URL length limits (2–8 KB). Use POST if Coroot adds support.
        """
        panel_cfg = json.dumps({
            "name": "",
            "source": {
                "metrics": {
                    "queries": [{"query": promql, "legend": legend}],
                },
            },
            "widget": {"chart": {"display": "line"}},
        })
        params: Dict[str, str] = {"query": panel_cfg}
        params.update(self._time_params(from_ts, to_ts))
        return self._get(
            f"/api/project/{project}/panel/data",
            params=params,
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
    # - Metric discovery: GET /prom/series and /prom/metadata help the
    #   agent explore unknown environments where metric names and label
    #   values aren't known upfront.
    # ------------------------------------------------------------------
