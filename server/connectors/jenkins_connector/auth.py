"""
Jenkins credential validation.

Validates API token credentials by calling the Jenkins root API endpoint.
Returns server metadata (version, mode, executor count) on success.
"""

import logging
import requests
from requests.auth import HTTPBasicAuth
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def validate_jenkins_credentials(
    base_url: str,
    username: str,
    api_token: str,
) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate Jenkins credentials by fetching server info.

    Args:
        base_url: Jenkins instance URL (e.g. https://jenkins.example.com)
        username: Jenkins username
        api_token: Jenkins API token

    Returns:
        Tuple of (success, server_info, error_message)
        server_info includes: version, mode, numExecutors, description, url
    """
    if not base_url or not username or not api_token:
        return False, None, "Jenkins URL, username, and API token are required"

    url = base_url.rstrip("/")

    try:
        response = requests.get(
            f"{url}/api/json",
            auth=HTTPBasicAuth(username, api_token),
            timeout=15,
            headers={"Accept": "application/json"},
        )

        if response.status_code == 401:
            return False, None, "Invalid credentials. Check your username and API token."

        if response.status_code == 403:
            return False, None, "Access denied. The user may lack read permissions."

        if response.status_code == 404:
            return False, None, "Jenkins API not found at this URL. Verify the Jenkins URL."

        if not response.ok:
            return False, None, f"Jenkins API error ({response.status_code})"

        data = response.json()

        # Extract version from response header if available
        version = response.headers.get("X-Jenkins", "unknown")

        server_info = {
            "version": version,
            "mode": data.get("mode", "unknown"),
            "numExecutors": data.get("numExecutors", 0),
            "description": data.get("description", ""),
            "url": data.get("url", url),
            "useCrumbs": data.get("useCrumbs", False),
            "useSecurity": data.get("useSecurity", True),
            "nodeDescription": data.get("nodeDescription", ""),
        }

        logger.info(
            "Jenkins credentials validated. Version: %s, Mode: %s",
            version,
            server_info["mode"],
        )
        return True, server_info, None

    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Verify the Jenkins URL is reachable."
    except requests.exceptions.ConnectionError:
        return False, None, "Cannot connect to Jenkins. Verify the URL and network access."
    except requests.exceptions.RequestException as e:
        logger.error("Jenkins validation request failed: %s", e)
        return False, None, f"Connection error: {str(e)}"
    except Exception as e:
        logger.error("Unexpected error validating Jenkins credentials: %s", e)
        return False, None, f"Unexpected error: {str(e)}"
