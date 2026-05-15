"""
Shared utilities for GitLab API integrations.

Provides common functionality for direct GitLab REST API calls
used by agent tools and route handlers.
"""

import json
import logging
import requests
from typing import Optional, Dict, Any

from utils.auth.stateless_auth import get_credentials_from_db

logger = logging.getLogger(__name__)

GITLAB_TIMEOUT = 30


def get_gitlab_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Get org-level GitLab credentials."""
    return get_credentials_from_db(user_id, "gitlab")


def is_gitlab_connected(user_id: str) -> bool:
    """Check if a user/org has valid GitLab credentials stored."""
    creds = get_gitlab_credentials(user_id)
    return bool(creds and creds.get("access_token"))


def gitlab_api_request(
    method: str,
    endpoint: str,
    user_id: str,
    params: Optional[Dict] = None,
    json_body: Optional[Dict] = None,
    timeout: int = GITLAB_TIMEOUT,
) -> Dict[str, Any]:
    """
    Make a GitLab API request using org-level credentials.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE)
        endpoint: API endpoint path (e.g., /projects/123/repository/commits)
        user_id: User ID for credential lookup
        params: Query parameters
        json_body: JSON body for POST/PUT
        timeout: Request timeout

    Returns:
        Dict with either parsed response or error key
    """
    creds = get_gitlab_credentials(user_id)
    if not creds or not creds.get("access_token"):
        return {"error": "No GitLab credentials configured. Ask an admin to connect GitLab in Settings > Connectors."}

    token = creds["access_token"]
    base_url = creds.get("base_url", "https://gitlab.com").rstrip("/")
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}

    url = f"{base_url}/api/v4{endpoint}"

    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )

        if resp.status_code >= 400:
            logger.error("GitLab API error %d for %s %s", resp.status_code, method, endpoint)
            return {"error": f"GitLab API error ({resp.status_code})"}

        if resp.status_code == 204:
            return {"success": True}

        return resp.json()
    except requests.RequestException as e:
        logger.error("GitLab request failed for %s %s: %s", method, endpoint, type(e).__name__)
        return {"error": f"GitLab request failed: {type(e).__name__}"}


def build_error_response(error: str, **kwargs) -> str:
    """Build a consistent JSON error response."""
    response = {"error": error, "success": False}
    response.update(kwargs)
    return json.dumps(response)


def build_success_response(**kwargs) -> str:
    """Build a consistent JSON success response."""
    response = {"success": True}
    response.update(kwargs)
    return json.dumps(response)
