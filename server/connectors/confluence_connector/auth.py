"""Authentication helpers for the Confluence connector."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

CONFLUENCE_AUTH_URL = "https://auth.atlassian.com/authorize"
CONFLUENCE_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
CONFLUENCE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

CONFLUENCE_SCOPES = (
    "read:page:confluence read:space:confluence read:user:confluence offline_access"
)
CONFLUENCE_AUDIENCE = "api.atlassian.com"

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
REDIRECT_URI = f"{FRONTEND_URL}/confluence/callback"


def _get_oauth_config() -> Dict[str, str]:
    return {
        "client_id": os.getenv("CONFLUENCE_CLIENT_ID", ""),
        "client_secret": os.getenv("CONFLUENCE_CLIENT_SECRET", ""),
        "redirect_uri": REDIRECT_URI,
        "audience": CONFLUENCE_AUDIENCE,
        "scopes": CONFLUENCE_SCOPES,
    }


def _validate_oauth_config() -> Dict[str, str]:
    config = _get_oauth_config()
    missing = [key for key in ("client_id", "client_secret") if not config[key]]
    if missing:
        raise ValueError(
            f"Confluence OAuth configuration missing: {', '.join(missing)}"
        )
    return config


def get_auth_url(state: str) -> str:
    """Generate the Atlassian OAuth 2.0 authorization URL."""
    if not state:
        raise ValueError("State parameter is required for Confluence OAuth.")

    config = _validate_oauth_config()
    params = {
        "audience": config["audience"],
        "client_id": config["client_id"],
        "scope": config["scopes"],
        "redirect_uri": config["redirect_uri"],
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }

    return f"{CONFLUENCE_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens."""
    config = _validate_oauth_config()
    payload = {
        "grant_type": "authorization_code",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "code": code,
        "redirect_uri": config["redirect_uri"],
    }

    response = requests.post(CONFLUENCE_TOKEN_URL, json=payload, timeout=30)
    if not response.ok:
        logger.error(
            "Confluence OAuth token exchange failed (%s): %s",
            response.status_code,
            response.text,
        )
    response.raise_for_status()
    token_data = response.json()

    if not token_data.get("access_token"):
        logger.error(
            "Confluence OAuth response missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("Confluence OAuth failed: missing access_token")

    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh Confluence OAuth access token using a refresh token."""
    if not refresh_token:
        raise ValueError("Confluence refresh_token is required")

    config = _validate_oauth_config()
    payload = {
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": refresh_token,
    }

    response = requests.post(CONFLUENCE_TOKEN_URL, json=payload, timeout=30)
    if not response.ok:
        logger.error(
            "Confluence OAuth refresh failed (%s): %s",
            response.status_code,
            response.text,
        )
    response.raise_for_status()
    token_data = response.json()

    access_token = token_data.get("access_token")
    if not access_token:
        logger.error(
            "Confluence OAuth refresh missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("Confluence OAuth refresh failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug(
                "Unable to compute expires_at for Confluence refresh response."
            )

    return token_data


def fetch_accessible_resources(access_token: str) -> List[Dict[str, Any]]:
    """Fetch Atlassian resources accessible by the OAuth token."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = requests.get(CONFLUENCE_RESOURCES_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def select_confluence_resource(
    resources: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Pick a Confluence resource from accessible resources."""
    for resource in resources:
        scopes = resource.get("scopes") or []
        if any("confluence" in scope for scope in scopes):
            return resource
    return resources[0] if resources else None
