"""
ThousandEyes API v7 client with OAuth Bearer token authentication.

Uses a Bearer token obtained from the ThousandEyes UI
(Account Settings > User API Tokens).

Use :func:`get_thousandeyes_client` to obtain a cached client instance
for a given user.  The cache keeps a single ``ThousandEyesClient`` per
user_id alive for ``CLIENT_CACHE_TTL`` seconds so that the underlying
``requests.Session`` is reused across Flask requests and agent tool calls.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

THOUSANDEYES_TIMEOUT = 30
CLIENT_CACHE_TTL = 36000  # 10 hours
BASE_URL = "https://api.thousandeyes.com/v7"

_client_cache: Dict[str, Tuple["ThousandEyesClient", float]] = {}
_cache_lock = threading.Lock()
_user_locks: Dict[str, threading.Lock] = {}
_SWEEP_INTERVAL: float = CLIENT_CACHE_TTL / 2


class ThousandEyesAPIError(Exception):
    """Custom error for ThousandEyes API interactions."""


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
    now = time.monotonic()
    last_sweep = getattr(_sweep_stale_locks, "_last_sweep", 0.0)
    if now - last_sweep < _SWEEP_INTERVAL:
        return
    _sweep_stale_locks._last_sweep = now
    stale = [uid for uid in _user_locks if uid not in _client_cache and not _user_locks[uid].locked()]
    for uid in stale:
        del _user_locks[uid]
    if stale:
        logger.debug("[THOUSANDEYES] Swept %d stale user locks", len(stale))


def get_thousandeyes_client(
    user_id: str,
    api_token: str,
    account_group_id: Optional[str] = None,
) -> "ThousandEyesClient":
    """Return a cached :class:`ThousandEyesClient` for *user_id*.

    A new client is created when no cached entry exists, the TTL has
    expired, or the credentials have changed.
    """
    user_lock = _get_user_lock(user_id)
    with user_lock:
        now = time.monotonic()
        with _cache_lock:
            entry = _client_cache.get(user_id)
        if entry is not None:
            client, created_at = entry
            creds_match = (
                client.api_token == api_token
                and client.account_group_id == account_group_id
            )
            if creds_match and (now - created_at) < CLIENT_CACHE_TTL:
                return client

        client = ThousandEyesClient(
            api_token=api_token,
            account_group_id=account_group_id,
            user_id=user_id,
        )
        # Validate the token by fetching account info
        try:
            client.get_account_status()
        except Exception:
            client._session.close()
            raise

        with _cache_lock:
            if entry is not None:
                previous_client = entry[0]
                try:
                    previous_client._session.close()
                except Exception:
                    logger.debug(
                        "[THOUSANDEYES] Failed to close session for user_id=%s (replacement)",
                        user_id,
                        exc_info=True,
                    )
            _client_cache[user_id] = (client, now)
            _sweep_stale_locks()
        return client


def invalidate_thousandeyes_client(user_id: str) -> None:
    """Remove *user_id*'s client from the cache (e.g. on disconnect)."""
    with _cache_lock:
        entry = _client_cache.pop(user_id, None)
        _user_locks.pop(user_id, None)
    if entry is not None:
        client = entry[0]
        try:
            client._session.close()
        except Exception:
            logger.debug(
                "[THOUSANDEYES] Failed to close session for user_id=%s",
                user_id,
                exc_info=True,
            )


class ThousandEyesClient:
    """HTTP client for ThousandEyes API v7.

    All requests use OAuth Bearer token authentication via the
    ``Authorization: Bearer <token>`` header.

    Prefer :func:`get_thousandeyes_client` over constructing this directly.
    """

    def __init__(
        self,
        api_token: str,
        account_group_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.api_token = api_token
        self.account_group_id = account_group_id
        self._user_id = user_id
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        """Send an authenticated request to ThousandEyes API v7."""
        kwargs.setdefault("timeout", THOUSANDEYES_TIMEOUT)
        url = f"{BASE_URL}{path}"

        # Add account group ID as query param if configured
        if self.account_group_id:
            params = kwargs.get("params", {}) or {}
            params["aid"] = self.account_group_id
            kwargs["params"] = params

        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            logger.error("[THOUSANDEYES] %s %s network error: %s", method, path, exc)
            raise ThousandEyesAPIError("Unable to reach ThousandEyes API") from exc

        if resp.status_code == 401:
            if self._user_id:
                invalidate_thousandeyes_client(self._user_id)
            raise ThousandEyesAPIError(
                "Invalid or expired Bearer token. Generate a new token in "
                "ThousandEyes > Account Settings > User API Tokens."
            )

        if resp.status_code == 429:
            remaining = resp.headers.get("X-Organization-Rate-Limit-Remaining", "0")
            raise ThousandEyesAPIError(
                f"ThousandEyes rate limit exceeded (240 req/min). "
                f"Remaining: {remaining}. Please wait before retrying."
            )

        if resp.status_code == 404:
            raise ThousandEyesAPIError(f"Resource not found: {path}")

        if not resp.ok:
            logger.error(
                "[THOUSANDEYES] %s %s failed (HTTP %s)",
                method, path, resp.status_code,
            )
            raise ThousandEyesAPIError(
                f"ThousandEyes API request failed (HTTP {resp.status_code})"
            )

        # Some endpoints return 204 No Content
        if resp.status_code == 204 or not resp.text:
            return {}

        try:
            return resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise ThousandEyesAPIError(f"Non-JSON response from {path}")

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    # ------------------------------------------------------------------
    # Account / Status
    # ------------------------------------------------------------------

    def get_account_status(self) -> Dict[str, Any]:
        """Validate the token by fetching account groups."""
        return self._get("/account-groups")

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def get_tests(self, test_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all configured tests, optionally filtered by type."""
        path = f"/tests/{test_type}" if test_type else "/tests"
        return self._get(path).get("tests", [])

    def get_test(self, test_id: str) -> Dict[str, Any]:
        """Get full configuration details for a single test."""
        return self._get(f"/tests/{test_id}")

    # ------------------------------------------------------------------
    # Test Results
    # ------------------------------------------------------------------

    # Maps user-facing result_type to the API path suffix
    _RESULT_TYPE_PATHS: Dict[str, str] = {
        "network": "network",
        "http": "http-server",
        "path-vis": "path-vis",
        "dns": "dns-server",
        "bgp": "bgp-routes",
        "page-load": "page-load",
        "web-transactions": "web-transactions",
        "ftp": "ftp-server",
        "api": "api",
        "sip": "sip-server",
        "voice": "voice",
        "dns-trace": "dns-trace",
        "dnssec": "dnssec",
    }

    def get_test_results(
        self, test_id: str, result_type: str = "network", window: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get results for a specific test.

        result_type: any key in ``_RESULT_TYPE_PATHS``.
        """
        suffix = self._RESULT_TYPE_PATHS.get(result_type, "network")
        params: Dict[str, str] = {}
        if window:
            params["window"] = window
        return self._get(f"/test-results/{test_id}/{suffix}", params=params)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def get_alerts(
        self,
        state: Optional[str] = None,
        severity: Optional[str] = None,
        window: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get alerts, optionally filtered by state and severity.

        Uses v7 field names: alertState, alertSeverity
        (state/severity deprecated May 2025).
        """
        params: Dict[str, str] = {}
        if state:
            params["alertState"] = state
        if severity:
            params["alertSeverity"] = severity
        if window:
            params["window"] = window
        data = self._get("/alerts", params=params)
        return data.get("alerts", [])

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def get_agents(
        self, agent_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List agents (cloud and enterprise).

        agent_type: 'cloud' or 'enterprise' to filter.
        """
        path = f"/agents/{agent_type}" if agent_type else "/agents"
        return self._get(path).get("agents", [])

    # ------------------------------------------------------------------
    # Internet Insights
    # ------------------------------------------------------------------

    def get_outages(
        self, outage_type: str = "network", window: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get outage data from Internet Insights.

        outage_type: 'network' (ISP/transit) or 'application' (SaaS/CDN).
        """
        suffix = "app" if outage_type == "application" else "net"
        params: Dict[str, str] = {}
        if window:
            params["window"] = window
        return self._get(f"/internet-insights/outages/{suffix}", params=params).get("outages", [])

    # ------------------------------------------------------------------
    # Alert Rules
    # ------------------------------------------------------------------

    def get_alert_rules(self) -> List[Dict[str, Any]]:
        """List all alert rule definitions."""
        return self._get("/alerts/rules").get("alertRules", [])

    # ------------------------------------------------------------------
    # Dashboards
    # ------------------------------------------------------------------

    def get_dashboards(self) -> List[Dict[str, Any]]:
        """List all dashboards."""
        data = self._get("/dashboards")
        if isinstance(data, list):
            return data
        return data.get("dashboards", [])

    def get_dashboard(self, dashboard_id: str) -> Dict[str, Any]:
        """Get a single dashboard including its widget list."""
        return self._get(f"/dashboards/{dashboard_id}")

    def get_dashboard_widget(
        self, dashboard_id: str, widget_id: str, window: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get data for a specific widget within a dashboard."""
        params: Dict[str, str] = {}
        if window:
            params["window"] = window
        return self._get(f"/dashboards/{dashboard_id}/widgets/{widget_id}", params=params)

    # ------------------------------------------------------------------
    # Endpoint Agents
    # ------------------------------------------------------------------

    def get_endpoint_agents(self) -> List[Dict[str, Any]]:
        """List endpoint agents (employee devices)."""
        return self._get("/endpoint/agents").get("agents", [])

    # ------------------------------------------------------------------
    # BGP Monitors
    # ------------------------------------------------------------------

    def get_bgp_monitors(self) -> List[Dict[str, Any]]:
        """List BGP monitoring points."""
        return self._get("/monitors").get("monitors", [])
