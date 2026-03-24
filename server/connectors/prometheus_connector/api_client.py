"""Prometheus and Alertmanager API clients.

PrometheusClient wraps the Prometheus server HTTP API (/api/v1/).
AlertmanagerClient wraps the Alertmanager API (/api/v2/).
Both support optional bearer-token and basic-auth authentication.
"""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15


class PrometheusAPIError(Exception):
    """Error raised by Prometheus server API calls."""


class AlertmanagerAPIError(Exception):
    """Error raised by Alertmanager API calls."""


# ---------------------------------------------------------------------------
# Prometheus server client  (/api/v1/)
# ---------------------------------------------------------------------------

class PrometheusClient:
    """Client for the Prometheus server HTTP API.

    Covers instant/range queries, alert rules, targets, metadata, and labels.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.username = username
        self.password = password
        self.timeout = timeout

    @property
    def _auth(self) -> Optional[tuple]:
        if self.username and self.password:
            return (self.username, self.password)
        return None

    @property
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url,
                headers=self._headers,
                auth=self._auth,
                params=params,
                data=data,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            logger.error("[PROMETHEUS] %s %s timeout", method, url)
            raise PrometheusAPIError("Connection timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[PROMETHEUS] %s %s connection error", method, url)
            raise PrometheusAPIError("Unable to reach Prometheus server") from exc
        except requests.RequestException as exc:
            logger.error("[PROMETHEUS] %s %s error: %s", method, url, exc)
            raise PrometheusAPIError("Unable to reach Prometheus server") from exc

        if resp.status_code == 401:
            raise PrometheusAPIError("Unauthorized: invalid credentials")
        if resp.status_code == 403:
            raise PrometheusAPIError("Forbidden: insufficient permissions")
        if resp.status_code == 429:
            raise PrometheusAPIError("Rate limit exceeded")

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[PROMETHEUS] %s %s failed (%s): %s",
                method, url, resp.status_code, resp.text[:300],
            )
            raise PrometheusAPIError(f"API error ({resp.status_code})") from exc

        body = resp.json()
        if body.get("status") == "error":
            raise PrometheusAPIError(body.get("error", "Unknown Prometheus error"))
        return body

    # -- Validation ----------------------------------------------------------

    def validate_connection(self) -> Dict[str, Any]:
        """Lightweight connectivity check via /api/v1/status/buildinfo."""
        body = self._request("GET", "/api/v1/status/buildinfo")
        data = body.get("data", {})
        return {"version": data.get("version", "unknown")}

    # -- Queries -------------------------------------------------------------

    def query(self, promql: str, time: Optional[str] = None) -> Dict[str, Any]:
        """Instant query via /api/v1/query."""
        params: Dict[str, Any] = {"query": promql}
        if time:
            params["time"] = time
        return self._request("POST", "/api/v1/query", data=params)

    def query_range(
        self,
        promql: str,
        start: str,
        end: str,
        step: str,
    ) -> Dict[str, Any]:
        """Range query via /api/v1/query_range."""
        return self._request("POST", "/api/v1/query_range", data={
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
        })

    # -- Alerts & Rules ------------------------------------------------------

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Firing alert rules from /api/v1/alerts."""
        body = self._request("GET", "/api/v1/alerts")
        return body.get("data", {}).get("alerts", [])

    def get_rules(self) -> Dict[str, Any]:
        """Alerting and recording rules from /api/v1/rules."""
        body = self._request("GET", "/api/v1/rules")
        return body.get("data", {})

    # -- Targets -------------------------------------------------------------

    def get_targets(self) -> Dict[str, Any]:
        """Scrape targets and health from /api/v1/targets."""
        body = self._request("GET", "/api/v1/targets")
        return body.get("data", {})

    # -- Metadata & Labels ---------------------------------------------------

    def get_metadata(self, metric: Optional[str] = None) -> Dict[str, Any]:
        """Metric metadata (type, help, unit) from /api/v1/metadata."""
        params: Dict[str, Any] = {}
        if metric:
            params["metric"] = metric
        body = self._request("GET", "/api/v1/metadata", params=params)
        return body.get("data", {})

    def get_labels(self) -> List[str]:
        """All label names from /api/v1/labels."""
        body = self._request("GET", "/api/v1/labels")
        return body.get("data", [])

    def get_label_values(self, label: str) -> List[str]:
        """Values for a label from /api/v1/label/{name}/values."""
        body = self._request("GET", f"/api/v1/label/{label}/values")
        return body.get("data", [])


# ---------------------------------------------------------------------------
# Alertmanager client  (/api/v2/)
# ---------------------------------------------------------------------------

class AlertmanagerClient:
    """Client for the Alertmanager HTTP API (v2).

    Covers firing alerts and silences.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.username = username
        self.password = password
        self.timeout = timeout

    @property
    def _auth(self) -> Optional[tuple]:
        if self.username and self.password:
            return (self.username, self.password)
        return None

    @property
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url,
                headers=self._headers,
                auth=self._auth,
                params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            logger.error("[ALERTMANAGER] %s %s timeout", method, url)
            raise AlertmanagerAPIError("Connection timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[ALERTMANAGER] %s %s connection error", method, url)
            raise AlertmanagerAPIError("Unable to reach Alertmanager") from exc
        except requests.RequestException as exc:
            logger.error("[ALERTMANAGER] %s %s error: %s", method, url, exc)
            raise AlertmanagerAPIError("Unable to reach Alertmanager") from exc

        if resp.status_code == 401:
            raise AlertmanagerAPIError("Unauthorized: invalid credentials")
        if resp.status_code == 403:
            raise AlertmanagerAPIError("Forbidden: insufficient permissions")

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[ALERTMANAGER] %s %s failed (%s): %s",
                method, url, resp.status_code, resp.text[:300],
            )
            raise AlertmanagerAPIError(f"API error ({resp.status_code})") from exc

        return resp.json()

    # -- Validation ----------------------------------------------------------

    def validate_connection(self) -> Dict[str, Any]:
        """Connectivity check via /api/v2/status."""
        data = self._request("GET", "/api/v2/status")
        return {
            "version": data.get("versionInfo", {}).get("version", "unknown"),
        }

    # -- Alerts --------------------------------------------------------------

    def get_alerts(
        self,
        silenced: Optional[bool] = None,
        inhibited: Optional[bool] = None,
        active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Firing alerts from /api/v2/alerts with optional filters."""
        params: Dict[str, Any] = {}
        if silenced is not None:
            params["silenced"] = str(silenced).lower()
        if inhibited is not None:
            params["inhibited"] = str(inhibited).lower()
        if active is not None:
            params["active"] = str(active).lower()
        return self._request("GET", "/api/v2/alerts", params=params)

    # -- Silences ------------------------------------------------------------

    def get_silences(self) -> List[Dict[str, Any]]:
        """Active silences from /api/v2/silences."""
        return self._request("GET", "/api/v2/silences")
