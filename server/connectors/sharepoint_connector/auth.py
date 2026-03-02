"""Authentication helpers for the SharePoint connector (Microsoft Entra ID OAuth 2.0)."""

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

AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

SCOPES = (
    "Sites.Read.All Files.Read.All User.Read "
    "offline_access openid profile email"
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
REDIRECT_URI = f"{FRONTEND_URL}/sharepoint/callback"


def _get_oauth_config() -> Dict[str, str]:
    return {
        "client_id": os.getenv("SHAREPOINT_CLIENT_ID", ""),
        "client_secret": os.getenv("SHAREPOINT_CLIENT_SECRET", ""),
        "tenant_id": os.getenv("SHAREPOINT_TENANT_ID", "common"),
        "redirect_uri": REDIRECT_URI,
        "scopes": SCOPES,
    }


def _validate_oauth_config() -> Dict[str, str]:
    config = _get_oauth_config()
    missing = [key for key in ("client_id", "client_secret") if not config[key]]
    if not FRONTEND_URL:
        missing.append("FRONTEND_URL")
    if missing:
        raise ValueError(
            f"SharePoint OAuth configuration missing: {', '.join(missing)}"
        )
    return config


def get_auth_url(state: str) -> str:
    """Generate the Microsoft Entra ID OAuth 2.0 authorization URL."""
    if not state:
        raise ValueError("State parameter is required for SharePoint OAuth.")

    config = _validate_oauth_config()
    tenant = config["tenant_id"]
    params = {
        "client_id": config["client_id"],
        "scope": config["scopes"],
        "redirect_uri": config["redirect_uri"],
        "state": state,
        "response_type": "code",
        "prompt": "select_account",
    }

    auth_url = AUTH_URL.format(tenant=tenant)
    return f"{auth_url}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens."""
    if not code:
        raise ValueError("Authorization code is required")
    config = _validate_oauth_config()
    tenant = config["tenant_id"]
    token_url = TOKEN_URL.format(tenant=tenant)

    payload = {
        "grant_type": "authorization_code",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "code": code,
        "redirect_uri": config["redirect_uri"],
        "scope": config["scopes"],
    }

    response = requests.post(token_url, data=payload, timeout=30)
    if not response.ok:
        logger.error(
            "SharePoint OAuth token exchange failed: status=%s",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    if not token_data.get("access_token"):
        logger.error(
            "SharePoint OAuth response missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("SharePoint OAuth failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug(
                "Unable to compute expires_at for SharePoint token response."
            )

    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh SharePoint OAuth access token using a refresh token."""
    if not refresh_token:
        raise ValueError("SharePoint refresh_token is required")

    config = _validate_oauth_config()
    tenant = config["tenant_id"]
    token_url = TOKEN_URL.format(tenant=tenant)

    payload = {
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": refresh_token,
        "scope": config["scopes"],
    }

    response = requests.post(token_url, data=payload, timeout=30)
    if not response.ok:
        logger.error(
            "SharePoint OAuth refresh failed: status=%s",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    access_token = token_data.get("access_token")
    if not access_token:
        logger.error(
            "SharePoint OAuth refresh missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("SharePoint OAuth refresh failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug(
                "Unable to compute expires_at for SharePoint refresh response."
            )

    return token_data


def fetch_user_profile(access_token: str) -> Dict[str, Any]:
    """Fetch the current user's profile from Microsoft Graph."""
    if not access_token:
        raise ValueError("access_token is required")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me", headers=headers, timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.error("Failed to fetch SharePoint user profile: %s", type(exc).__name__)
        raise


def list_sharepoint_sites(
    access_token: str, search: str = ""
) -> List[Dict[str, Any]]:
    """List SharePoint sites accessible by the OAuth token.

    Args:
        access_token: Valid Microsoft Graph access token.
        search: Optional search query to filter sites.

    Returns:
        List of site dictionaries from the Graph API.
    """
    if not access_token:
        raise ValueError("access_token is required")
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params: Dict[str, str] = {}
    if search:
        params["search"] = search

    try:
        response = requests.get(
            "https://graph.microsoft.com/v1.0/sites",
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("value", [])
    except requests.RequestException as exc:
        logger.error("Failed to list SharePoint sites: %s", type(exc).__name__)
        raise
