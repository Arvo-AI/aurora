"""
Cloudflare API token validation.

Supports both user-owned tokens and account-owned tokens (cfat_ prefix).
"""

import logging
import requests
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


def _discover_account_id(headers: Dict[str, str]) -> Optional[str]:
    """Use GET /accounts to find the account this token belongs to."""
    try:
        response = requests.get(
            f"{CLOUDFLARE_API_BASE}/accounts",
            headers=headers,
            timeout=15,
        )
        if response.ok:
            accounts = response.json().get("result", [])
            if accounts:
                return accounts[0].get("id")
    except Exception as e:
        logger.warning(f"Failed to discover account ID: {e}")
    return None


def _verify_token(headers: Dict[str, str], account_id: Optional[str] = None) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Verify a token via the account or user verify endpoint."""
    endpoint = (f"/accounts/{account_id}/tokens/verify" if account_id
                else "/user/tokens/verify")
    try:
        response = requests.get(
            f"{CLOUDFLARE_API_BASE}{endpoint}",
            headers=headers,
            timeout=15,
        )

        if response.status_code == 401:
            return False, None, "Invalid API token. Please check the token and try again."
        if response.status_code == 403:
            return False, None, "Access denied. The token may have been revoked."
        if not response.ok:
            logger.warning(f"Token verification failed ({response.status_code})")
            return False, None, "Token verification failed. Please check the token and try again."

        data = response.json()
        result = data.get("result", {})
        status = result.get("status")

        if status != "active":
            return False, None, "Token is not active. Please generate a new token."

        return True, {"token_id": result.get("id"), "status": status}, None

    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"Token verification failed: {e}")
        return False, None, "Connection error. Please check your network and try again."


def validate_api_token(api_token: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate a Cloudflare API token. Handles both user-owned and account-owned
    tokens (cfat_ prefix) automatically.

    Returns:
        Tuple of (success, token_info, error_message)
        token_info includes: token_id, status, and for account tokens: account_id
    """
    if not api_token:
        return False, None, "API token is required"

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    is_account_token = api_token.startswith("cfat_")

    if is_account_token:
        account_id = _discover_account_id(headers)
        if not account_id:
            return False, None, "Could not determine the account for this token. Verify it has account-level access."

        success, token_info, error = _verify_token(headers, account_id)
        if success and token_info:
            token_info["account_id"] = account_id
        return success, token_info, error

    return _verify_token(headers)
