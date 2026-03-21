"""
Cloudflare API token validation.
"""

import logging
import requests
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


def validate_api_token(api_token: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate a Cloudflare API token by calling the token verification endpoint.

    Args:
        api_token: Cloudflare API token (fine-grained, not the legacy Global API Key)

    Returns:
        Tuple of (success, token_info, error_message)
        token_info includes: token_id, status
    """
    if not api_token:
        return False, None, "API token is required"

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(
            f"{CLOUDFLARE_API_BASE}/user/tokens/verify",
            headers=headers,
            timeout=15,
        )

        if response.status_code == 401:
            return False, None, "Invalid API token. Please check the token and try again."

        if response.status_code == 403:
            return False, None, "Access denied. The token may have been revoked."

        if not response.ok:
            try:
                data = response.json()
                errors = data.get("errors", [])
                error_detail = errors[0].get("message", "") if errors else response.text[:200]
            except Exception:
                error_detail = response.text[:200]
            logger.warning(f"Cloudflare token verification failed ({response.status_code}): {error_detail}")
            return False, None, "Token verification failed. Please check the token and try again."

        data = response.json()
        result = data.get("result", {})
        status = result.get("status")

        if status != "active":
            logger.warning(f"Cloudflare token not active (status: {status})")
            return False, None, "Token is not active. Please generate a new token."

        token_info = {
            "token_id": result.get("id"),
            "status": status,
        }

        logger.info("Cloudflare API token validated successfully")
        return True, token_info, None

    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"Cloudflare token verification failed: {e}")
        return False, None, "Connection error. Please check your network and try again."
    except Exception as e:
        logger.error(f"Unexpected error verifying Cloudflare token: {e}")
        return False, None, "An unexpected error occurred. Please try again."
