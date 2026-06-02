"""
Fly.io REST API client for auth validation and Prometheus metrics.

Used by the connector auth layer and the agent's metrics tool.
The agent uses flyctl CLI via cloud_exec for all other interactions.
"""

import logging
import requests
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

FLYIO_MACHINES_API = "https://api.machines.dev/v1"
FLYIO_PROMETHEUS_API = "https://api.fly.io/prometheus"


class FlyioClient:
    """REST client for Fly.io Machines API and Prometheus federation endpoint."""

    def __init__(self, api_token: str, org_slug: str):
        self.api_token = api_token
        self.org_slug = org_slug

        auth_value = api_token if api_token.startswith("FlyV1") else f"Bearer {api_token}"
        self._headers = {
            "Authorization": auth_value,
            "Content-Type": "application/json",
        }

    def list_apps(self) -> Optional[List[Dict[str, Any]]]:
        """List all apps in the organization. Returns None on auth/network failure."""
        try:
            response = requests.get(
                f"{FLYIO_MACHINES_API}/apps",
                headers=self._headers,
                params={"org_slug": self.org_slug},
                timeout=15,
            )
            if not response.ok:
                logger.warning(f"Fly.io list_apps failed ({response.status_code})")
                return None
            data = response.json()
            return data if isinstance(data, list) else data.get("apps", [])
        except Exception as e:
            logger.error(f"Fly.io list_apps error: {e}")
            return None

    def has_write_access(self) -> bool:
        """Probe whether the token has write access by attempting an invalid app create."""
        try:
            response = requests.post(
                f"{FLYIO_MACHINES_API}/apps",
                headers=self._headers,
                json={"app_name": "", "org_slug": self.org_slug},
                timeout=10,
            )
            return response.status_code in (400, 422)
        except Exception as e:
            logger.warning(f"Fly.io write access probe failed: {e}")
            return False

    def query_prometheus(self, query: str, time_param: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Execute a PromQL instant query against the Fly.io Prometheus federation.

        Args:
            query: PromQL expression (e.g. 'fly_instance_up{app="myapp"}')
            time_param: Optional RFC3339 or Unix timestamp for evaluation time
        """
        try:
            params: Dict[str, str] = {"query": query}
            if time_param:
                params["time"] = time_param

            response = requests.get(
                f"{FLYIO_PROMETHEUS_API}/{self.org_slug}/api/v1/query",
                headers=self._headers,
                params=params,
                timeout=20,
            )
            if not response.ok:
                logger.warning(f"Fly.io prometheus query failed ({response.status_code}): {query[:100]}")
                return None
            return response.json()
        except Exception as e:
            logger.error(f"Fly.io prometheus query error: {e}")
            return None
