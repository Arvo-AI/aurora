"""
Cloudflare API client.

Provides an authenticated interface to the Cloudflare v4 API for use by
Aurora's route handlers and (eventually) agent tools.
"""

import logging
import requests
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    """Authenticated Cloudflare API client."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, json_data: Optional[Dict] = None,
                  params: Optional[Dict] = None, timeout: int = 15) -> Dict[str, Any]:
        response = requests.request(
            method,
            f"{CLOUDFLARE_API_BASE}{path}",
            headers=self.headers,
            json=json_data,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def list_accounts(self) -> List[Dict]:
        """List all accounts the token has access to."""
        data = self._request("GET", "/accounts", params={"per_page": 50})
        return data.get("result", [])

    def get_current_user(self) -> Dict:
        """Get the user associated with the current token."""
        data = self._request("GET", "/user")
        return data.get("result", {})

    def list_zones(self, account_id: Optional[str] = None) -> List[Dict]:
        """List all DNS zones, optionally filtered by account. Paginates automatically."""
        all_zones: List[Dict] = []
        page = 1

        while True:
            params: Dict[str, Any] = {"per_page": 50, "page": page}
            if account_id:
                params["account.id"] = account_id

            data = self._request("GET", "/zones", params=params)
            all_zones.extend(data.get("result", []))

            total_pages = data.get("result_info", {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return all_zones
