"""
Prometheus HTTP API client.

Provides a typed client for querying Prometheus-compatible endpoints
(Prometheus, Thanos, Cortex, Mimir, VictoriaMetrics).
"""

import logging
from typing import Any, Dict, List, Optional

from .base_client import BaseHTTPClient, APIError, build_auth_headers

logger = logging.getLogger(__name__)

PrometheusAPIError = APIError

MAX_SERIES = 500


class PrometheusClient(BaseHTTPClient):
    """HTTP client for the Prometheus query API."""

    def __init__(
        self,
        prometheus_url: str,
        auth_type: str = "none",
        username: Optional[str] = None,
        password: Optional[str] = None,
        bearer_token: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        verify_ssl: bool = True,
        timeout: int = 30,
    ):
        auth_headers = build_auth_headers(
            auth_type=auth_type,
            username=username,
            password=password,
            bearer_token=bearer_token,
            custom_headers=custom_headers,
        )
        super().__init__(
            base_url=prometheus_url,
            auth_headers=auth_headers,
            verify_ssl=verify_ssl,
            read_timeout=timeout,
        )

    def _prom_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Prometheus-specific request that handles the status/error envelope."""
        result = self._request(method, path, params=params, data=data, retries=retries)

        if isinstance(result, dict) and result.get("status") == "error":
            error_type = result.get("errorType", "unknown")
            error_msg = result.get("error", "Unknown error")
            raise APIError(f"Prometheus {error_type}: {error_msg}")

        return result or {}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_connection(self) -> Dict[str, Any]:
        """Validate connectivity and auto-detect backend type.

        Uses no retries — fail fast during connection setup.
        """
        try:
            result = self._prom_request("GET", "/api/v1/status/buildinfo", retries=0)
            data = result.get("data", {})
            version = data.get("version", "")
            if "victoriametrics" in version.lower():
                data["_backend"] = "victoriametrics"
            elif "thanos" in version.lower():
                data["_backend"] = "thanos"
            else:
                data["_backend"] = "prometheus"
            return data
        except APIError:
            # Fallback: some backends don't expose buildinfo
            try:
                self._prom_request("POST", "/api/v1/query", data={"query": "1"}, retries=0)
                return {
                    "version": "unknown",
                    "_backend": "prometheus-compatible",
                    "_validated_via": "query",
                }
            except APIError:
                raise

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query_instant(self, promql: str, time: Optional[str] = None) -> Dict[str, Any]:
        """Execute an instant PromQL query (single point in time)."""
        params: Dict[str, Any] = {"query": promql}
        if time:
            params["time"] = time

        result = self._prom_request("POST", "/api/v1/query", data=params)
        data = result.get("data", {})
        self._cap_series(data)
        return data

    def query_range(
        self,
        promql: str,
        start: str,
        end: str,
        step: str = "60s",
    ) -> Dict[str, Any]:
        """Execute a range PromQL query (time series over interval)."""
        params: Dict[str, Any] = {
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
        }

        result = self._prom_request("POST", "/api/v1/query_range", data=params)
        data = result.get("data", {})
        self._cap_series(data)
        return data

    # ------------------------------------------------------------------
    # Alerts & Rules
    # ------------------------------------------------------------------

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Get currently firing alerts from Prometheus alerting rules."""
        result = self._prom_request("GET", "/api/v1/alerts")
        return result.get("data", {}).get("alerts", [])

    def get_rules(self, rule_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get alerting and/or recording rules."""
        params: Dict[str, str] = {}
        if rule_type:
            params["type"] = rule_type

        result = self._prom_request("GET", "/api/v1/rules", params=params or None)
        return result.get("data", {}).get("groups", [])

    # ------------------------------------------------------------------
    # Targets & Metadata
    # ------------------------------------------------------------------

    def get_targets(self, state: Optional[str] = None) -> Dict[str, Any]:
        """Get scrape targets and their health status."""
        params: Dict[str, str] = {}
        if state:
            params["state"] = state

        result = self._prom_request("GET", "/api/v1/targets", params=params or None)
        return result.get("data", {})

    def get_metadata(self, metric: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """Get metric metadata (type, help, unit)."""
        params: Dict[str, Any] = {"limit": str(limit)}
        if metric:
            params["metric"] = metric

        result = self._prom_request("GET", "/api/v1/metadata", params=params)
        return result.get("data", {})

    def get_label_values(self, label: str) -> List[str]:
        """Get all values for a given label name."""
        result = self._prom_request("GET", f"/api/v1/label/{label}/values")
        return result.get("data", [])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cap_series(data: Dict[str, Any]) -> None:
        """Truncate large result sets to prevent memory issues."""
        results = data.get("result", [])
        if len(results) > MAX_SERIES:
            data["result"] = results[:MAX_SERIES]
            data["_truncated"] = True
            data["_total_series"] = len(results)
