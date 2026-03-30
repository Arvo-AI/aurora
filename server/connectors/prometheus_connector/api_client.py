"""Prometheus & Alertmanager API client.

Wraps the Prometheus HTTP API and Alertmanager API with authentication,
timeout handling, and convenience methods for validation, alert retrieval,
and metric querying.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

PROMETHEUS_TIMEOUT = 20


class PrometheusAPIError(Exception):
    """Custom error for Prometheus API interactions."""


class PrometheusClient:
    """Prometheus HTTP API client.

    Supports both unauthenticated and Bearer-token-authenticated instances.
    Can connect to standalone Prometheus or Alertmanager endpoints.
    """

    def __init__(self, base_url: str, api_token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

    @property
    def headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers,
                timeout=PROMETHEUS_TIMEOUT, **kwargs,
            )
        except requests.exceptions.Timeout as exc:
            logger.error("[PROMETHEUS] %s %s timeout", method, url)
            raise PrometheusAPIError("Connection timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[PROMETHEUS] %s %s connection error", method, url)
            raise PrometheusAPIError(
                f"Unable to reach Prometheus at {self.base_url}"
            ) from exc
        except requests.RequestException as exc:
            logger.error("[PROMETHEUS] %s %s error: %s", method, url, exc)
            raise PrometheusAPIError(
                f"Unable to reach Prometheus at {self.base_url}"
            ) from exc

        if response.status_code == 401:
            raise PrometheusAPIError("Unauthorized: check your API token")
        if response.status_code == 403:
            raise PrometheusAPIError("Forbidden: token lacks required permissions")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[PROMETHEUS] %s %s failed (%s): %s",
                method, url, response.status_code, response.text[:200],
            )
            raise PrometheusAPIError(f"API error ({response.status_code})") from exc

        return response

    # --- Validation ---

    def validate(self) -> Dict[str, Any]:
        """Validate connectivity by querying the build info endpoint."""
        resp = self._request("GET", "/api/v1/status/buildinfo")
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusAPIError("Unexpected response from Prometheus")
        build = data.get("data", {})
        return {
            "valid": True,
            "version": build.get("version", "unknown"),
            "goVersion": build.get("goVersion"),
        }

    # --- Alerts ---

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Fetch currently firing alerts from /api/v1/alerts."""
        resp = self._request("GET", "/api/v1/alerts")
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusAPIError("Failed to fetch alerts")
        return data.get("data", {}).get("alerts", [])

    def get_rules(self) -> List[Dict[str, Any]]:
        """Fetch alerting and recording rules."""
        resp = self._request("GET", "/api/v1/rules")
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusAPIError("Failed to fetch rules")
        return data.get("data", {}).get("groups", [])

    # --- Targets ---

    def get_targets(self) -> Dict[str, Any]:
        """Fetch scrape target status."""
        resp = self._request("GET", "/api/v1/targets")
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusAPIError("Failed to fetch targets")
        return data.get("data", {})

    # --- Query ---

    def query(self, promql: str) -> Dict[str, Any]:
        """Execute an instant PromQL query."""
        resp = self._request("GET", "/api/v1/query", params={"query": promql})
        data = resp.json()
        if data.get("status") != "success":
            raise PrometheusAPIError(
                f"Query failed: {data.get('error', 'unknown error')}"
            )
        return data.get("data", {})
