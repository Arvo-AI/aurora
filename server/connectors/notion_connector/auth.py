"""Authentication helpers for the Notion connector (OAuth 2.0 + Internal Integration Tokens)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

AUTHORIZE_URL = f"{NOTION_API_BASE}/oauth/authorize"
TOKEN_URL = f"{NOTION_API_BASE}/oauth/token"
REVOKE_URL = f"{NOTION_API_BASE}/oauth/revoke"
INTROSPECT_URL = f"{NOTION_API_BASE}/oauth/introspect"
USERS_ME_URL = f"{NOTION_API_BASE}/users/me"


def _get_oauth_config() -> Dict[str, str]:
    """Read Notion OAuth configuration from environment variables."""
    frontend_url = os.getenv("FRONTEND_URL", "")
    redirect_uri = os.getenv("NOTION_REDIRECT_URI") or (
        f"{frontend_url}/notion/callback" if frontend_url else ""
    )
    return {
        "client_id": os.getenv("NOTION_CLIENT_ID", ""),
        "client_secret": os.getenv("NOTION_CLIENT_SECRET", ""),
        "redirect_uri": redirect_uri,
    }


def _validate_oauth_config() -> Dict[str, str]:
    config = _get_oauth_config()
    missing = [
        key for key in ("client_id", "client_secret", "redirect_uri") if not config[key]
    ]
    if missing:
        raise ValueError(
            f"Notion OAuth configuration missing: {', '.join(missing)}"
        )
    return config


def is_oauth_configured() -> bool:
    """True when client id + secret + redirect URI are all available."""
    config = _get_oauth_config()
    return bool(
        config["client_id"] and config["client_secret"] and config["redirect_uri"]
    )


def get_auth_url(state: str) -> str:
    """Generate the Notion OAuth authorization URL."""
    if not state:
        raise ValueError("State parameter is required for Notion OAuth.")

    config = _validate_oauth_config()
    params = {
        "client_id": config["client_id"],
        "response_type": "code",
        "owner": "user",
        "redirect_uri": config["redirect_uri"],
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _token_headers() -> Dict[str, str]:
    return {
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange an OAuth authorization code for access + refresh tokens."""
    if not code:
        raise ValueError("Authorization code is required")

    config = _validate_oauth_config()
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config["redirect_uri"],
    }

    response = requests.post(
        TOKEN_URL,
        auth=(config["client_id"], config["client_secret"]),
        headers=_token_headers(),
        json=body,
        timeout=30,
    )
    if not response.ok:
        logger.error(
            "Notion OAuth token exchange failed: status=%s",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    if not token_data.get("access_token"):
        logger.error("Notion OAuth response missing access_token")
        raise ValueError("Notion OAuth failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug("Unable to compute expires_at for Notion token response.")

    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh a Notion OAuth access token using a refresh token."""
    if not refresh_token:
        raise ValueError("Notion refresh_token is required")

    config = _validate_oauth_config()
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    response = requests.post(
        TOKEN_URL,
        auth=(config["client_id"], config["client_secret"]),
        headers=_token_headers(),
        json=body,
        timeout=30,
    )
    if not response.ok:
        logger.error(
            "Notion OAuth refresh failed: status=%s",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    if not token_data.get("access_token"):
        logger.error("Notion OAuth refresh missing access_token")
        raise ValueError("Notion OAuth refresh failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug("Unable to compute expires_at for Notion refresh response.")

    return token_data


def revoke_token(token: str) -> None:
    """Best-effort revoke of a Notion OAuth token; errors are logged but swallowed."""
    if not token:
        return
    try:
        config = _validate_oauth_config()
    except ValueError as exc:
        logger.warning("Notion revoke_token skipped: %s", exc)
        return

    try:
        response = requests.post(
            REVOKE_URL,
            auth=(config["client_id"], config["client_secret"]),
            headers=_token_headers(),
            json={"token": token},
            timeout=15,
        )
        if not response.ok:
            logger.warning(
                "Notion token revoke returned status=%s", response.status_code
            )
    except requests.RequestException as exc:
        logger.warning("Notion token revoke failed: %s", type(exc).__name__)


def introspect_token(token: str) -> Dict[str, Any]:
    """Introspect a Notion OAuth token. Returns the raw JSON response on success."""
    if not token:
        raise ValueError("token is required")

    config = _validate_oauth_config()
    response = requests.post(
        INTROSPECT_URL,
        auth=(config["client_id"], config["client_secret"]),
        headers=_token_headers(),
        json={"token": token},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def validate_internal_integration_token(token: str) -> Dict[str, Any]:
    """Validate an Internal Integration Token by calling /users/me.

    Returns the JSON payload (bot + owner). Raises ``requests.HTTPError`` on
    non-2xx responses.
    """
    if not token:
        raise ValueError("token is required")

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Accept": "application/json",
    }
    response = requests.get(USERS_ME_URL, headers=headers, timeout=15)
    if not response.ok:
        logger.error(
            "Notion IIT validation failed: status=%s",
            response.status_code,
        )
    response.raise_for_status()
    return response.json()
