"""
Alertmanager HTTP API v2 client.

Provides methods for querying alerts, managing silences,
and checking Alertmanager status.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .base_client import BaseHTTPClient, APIError

logger = logging.getLogger(__name__)

AlertmanagerAPIError = APIError


class AlertmanagerClient(BaseHTTPClient):
    """HTTP client for the Alertmanager v2 API."""

    def __init__(
        self,
        alertmanager_url: str,
        auth_headers: Optional[Dict[str, str]] = None,
        verify_ssl: bool = True,
    ):
        super().__init__(
            base_url=alertmanager_url,
            auth_headers=auth_headers,
            verify_ssl=verify_ssl,
            read_timeout=15,
        )

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self._auth_headers,
        }

    # ------------------------------------------------------------------
    # Status / Validation
    # ------------------------------------------------------------------

    def validate_connection(self) -> Dict[str, Any]:
        """Validate connectivity to Alertmanager via the status endpoint."""
        result = self._request("GET", "/api/v2/status", retries=0)
        return {
            "version": result.get("versionInfo", {}).get("version", "unknown"),
            "uptime": result.get("uptime"),
            "cluster_status": result.get("cluster", {}).get("status"),
        }

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def get_alerts(
        self,
        active: bool = True,
        silenced: bool = False,
        inhibited: bool = False,
        filter_matchers: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get alerts from Alertmanager.

        filter_matchers: PromQL-style matchers e.g. ['alertname="HighCPU"', 'severity="critical"']
        """
        params: Dict[str, Any] = {
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
        }
        if filter_matchers:
            params["filter"] = filter_matchers

        result = self._request("GET", "/api/v2/alerts", params=params)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Silences
    # ------------------------------------------------------------------

    def get_silences(self, filter_matchers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Get active/pending silences."""
        params: Dict[str, Any] = {}
        if filter_matchers:
            params["filter"] = filter_matchers

        result = self._request("GET", "/api/v2/silences", params=params)
        silences = result if isinstance(result, list) else []
        return [s for s in silences if s.get("status", {}).get("state") in ("active", "pending")]

    def create_silence(
        self,
        matchers: List[Dict[str, Any]],
        duration_minutes: int = 60,
        created_by: str = "Aurora AI",
        comment: str = "Silenced during incident investigation",
    ) -> Dict[str, Any]:
        """Create a new silence. Max duration: 24 hours."""
        if not matchers:
            raise APIError("At least one matcher is required")

        duration_minutes = min(duration_minutes, 1440)
        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(minutes=duration_minutes)

        payload = {
            "matchers": matchers,
            "startsAt": now.isoformat(),
            "endsAt": ends_at.isoformat(),
            "createdBy": created_by,
            "comment": comment,
        }

        result = self._request("POST", "/api/v2/silences", json_body=payload)
        silence_id = result.get("silenceID") if isinstance(result, dict) else None

        return {
            "silenceId": silence_id,
            "startsAt": now.isoformat(),
            "endsAt": ends_at.isoformat(),
            "matchers": matchers,
            "comment": comment,
        }

    def expire_silence(self, silence_id: str) -> bool:
        """Expire (delete) an active silence by ID."""
        if not silence_id:
            raise APIError("silence_id is required")
        self._request("DELETE", f"/api/v2/silence/{silence_id}")
        return True
