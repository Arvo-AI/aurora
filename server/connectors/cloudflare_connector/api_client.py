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

    def _extract_permission_names(self, policies: List[Dict]) -> List[str]:
        names: List[str] = []
        for policy in policies:
            if policy.get("effect") != "allow":
                continue
            for group in policy.get("permission_groups", []):
                name = group.get("name")
                if name:
                    names.append(name)
        return sorted(set(names))

    def get_token_permissions(self, token_id: str, account_id: Optional[str] = None) -> List[str]:
        """
        Fetch permission group names granted to this token.

        Tries the account-level endpoint first (for account-owned tokens),
        then falls back to the user-level endpoint (for user-owned tokens).
        """
        if account_id:
            try:
                data = self._request("GET", f"/accounts/{account_id}/tokens/{token_id}")
                return self._extract_permission_names(
                    data.get("result", {}).get("policies", []))
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (403, 404):
                    logger.info("Account token lookup failed, trying user token endpoint")
                else:
                    logger.warning(f"Failed to fetch account token permissions: {e}")

        try:
            data = self._request("GET", f"/user/tokens/{token_id}")
            return self._extract_permission_names(
                data.get("result", {}).get("policies", []))
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.warning("Token lacks permission to read its own details")
            else:
                logger.warning(f"Failed to fetch token permissions: {e}")
            return []
        except Exception as e:
            logger.warning(f"Failed to fetch token permissions: {e}")
            return []
