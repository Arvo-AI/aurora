"""BigPanda Cloud API client.

Wraps the BigPanda REST API with authentication, rate-limit awareness,
and convenience methods for token validation and incident retrieval.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

BIGPANDA_API_BASE = "https://api.bigpanda.io"
BIGPANDA_TIMEOUT = 20


class BigPandaAPIError(Exception):
    """Custom error for BigPanda API interactions."""


class BigPandaClient:
    """BigPanda REST API client.

    Authentication: Bearer token (User API Key or Org-level token).
    Rate limits: 150 req/min (Incidents V2), 5 req/sec (general).
    """

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = BIGPANDA_API_BASE

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers,
                timeout=BIGPANDA_TIMEOUT, **kwargs,
            )
        except requests.exceptions.Timeout as exc:
            logger.error("[BIGPANDA] %s %s timeout", method, url)
            raise BigPandaAPIError("Connection timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[BIGPANDA] %s %s connection error", method, url)
            raise BigPandaAPIError("Unable to reach BigPanda") from exc
        except requests.RequestException as exc:
            logger.error("[BIGPANDA] %s %s error: %s", method, url, exc)
            raise BigPandaAPIError("Unable to reach BigPanda") from exc

        if response.status_code == 429:
            raise BigPandaAPIError("BigPanda API rate limit reached")
        if response.status_code == 401:
            raise BigPandaAPIError("Unauthorized: invalid or expired API token")
        if response.status_code == 403:
            raise BigPandaAPIError("Forbidden: token lacks required permissions")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[BIGPANDA] %s %s failed (%s): %s",
                method, url, response.status_code, response.text[:200],
            )
            raise BigPandaAPIError(f"API error ({response.status_code})") from exc

        return response

    # --- Validation ---

    def validate_token(self) -> Dict[str, Any]:
        """Validate the API token by fetching environments (lightweight call)."""
        resp = self._request("GET", "/resources/v2.0/environments")
        data = resp.json()
        env_list = data if isinstance(data, list) else []
        return {"valid": True, "environment_count": len(env_list)}

    # --- Incidents ---

    def get_incidents(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch incidents from Incidents V2 API."""
        params: Dict[str, Any] = {"limit": min(limit, 100), "offset": offset}
        if status:
            params["status"] = status
        return self._request("GET", "/resources/v2.0/incidents", params=params).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        """Fetch a single incident with full alert details."""
        return self._request("GET", f"/resources/v2.0/incidents/{incident_id}").json()

    # --- Environments ---

    def get_environments(self) -> List[Dict[str, Any]]:
        """Fetch all environments."""
        resp = self._request("GET", "/resources/v2.0/environments")
        data = resp.json()
        return data if isinstance(data, list) else []
