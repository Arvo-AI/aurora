"""Rootly API client.

Wraps the Rootly REST API (JSON:API spec) with authentication,
rate-limit awareness, and convenience methods for token validation
and incident retrieval.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

ROOTLY_API_BASE = "https://api.rootly.com"
ROOTLY_TIMEOUT = 20


class RootlyAPIError(Exception):
    """Custom error for Rootly API interactions."""


class RootlyClient:
    """Rootly REST API client.

    Authentication: Bearer token (Global, Team, or Personal API key).
    Rate limits: 3000 GET/min, 3000 write/min, 50 alerts/min.
    """

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = ROOTLY_API_BASE

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers,
                timeout=ROOTLY_TIMEOUT, **kwargs,
            )
        except requests.exceptions.Timeout as exc:
            logger.error("[ROOTLY] %s %s timeout", method, url)
            raise RootlyAPIError("Connection timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[ROOTLY] %s %s connection error", method, url)
            raise RootlyAPIError("Unable to reach Rootly") from exc
        except requests.RequestException as exc:
            logger.error("[ROOTLY] %s %s error: %s", method, url, exc)
            raise RootlyAPIError("Unable to reach Rootly") from exc

        if response.status_code == 429:
            raise RootlyAPIError("Rootly API rate limit reached")
        if response.status_code == 401:
            raise RootlyAPIError("Unauthorized: invalid or expired API token")
        if response.status_code == 403:
            raise RootlyAPIError("Forbidden: token lacks required permissions")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[ROOTLY] %s %s failed (%s): %s",
                method, url, response.status_code, response.text[:200],
            )
            raise RootlyAPIError(f"API error ({response.status_code})") from exc

        return response

    def validate_token(self) -> Dict[str, Any]:
        """Validate the API token by fetching the current user's authorizations."""
        resp = self._request("GET", "/v1/authorizations")
        data = resp.json()
        items = data.get("data", [])
        return {"valid": True, "authorization_count": len(items)}

    def get_current_user(self) -> Dict[str, Any]:
        """Fetch current user info."""
        resp = self._request("GET", "/v1/authorizations")
        data = resp.json()
        items = data.get("data", [])
        if items:
            first = items[0]
            attrs = first.get("attributes", {})
            return {
                "email": attrs.get("email"),
                "name": attrs.get("name"),
            }
        return {}

    def get_incidents(
        self,
        page_size: int = 25,
        page_number: int = 1,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch incidents from Rootly."""
        params: Dict[str, Any] = {
            "page[size]": min(page_size, 100),
            "page[number]": max(1, page_number),
        }
        if status:
            params["filter[status]"] = status
        return self._request("GET", "/v1/incidents", params=params).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        """Fetch a single incident."""
        return self._request("GET", f"/v1/incidents/{incident_id}").json()

    def get_services(self, page_size: int = 10) -> List[Dict[str, Any]]:
        """Fetch services."""
        resp = self._request("GET", "/v1/services", params={"page[size]": page_size})
        data = resp.json()
        return data.get("data", [])
