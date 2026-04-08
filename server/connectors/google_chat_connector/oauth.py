"""
Google Chat OAuth 2.0 configuration for Aurora integration.

Uses Google's OAuth2 flow to authenticate users and obtain credentials
for the Google Chat API. The Chat app itself uses a GCP service account,
but we use OAuth to link a Google Workspace user to their Aurora account.
"""

import os
import logging
from typing import Dict, Any
from urllib.parse import urlencode
from dotenv import load_dotenv
import requests

load_dotenv()

logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv("GOOGLE_CHAT_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CHAT_CLIENT_SECRET")

backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
if not backend_url:
    logger.warning("NEXT_PUBLIC_BACKEND_URL not set - Google Chat OAuth callbacks will not work")

REDIRECT_URI = f"{backend_url}/google-chat/callback"

GOOGLE_CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.memberships",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.create",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def get_auth_url(state: str) -> str:
    """Generate the Google OAuth authorization URL with state parameter."""
    if not state:
        raise ValueError("State parameter is required for Google Chat OAuth.")

    if not CLIENT_ID or not CLIENT_SECRET:
        raise ValueError(
            "Google Chat OAuth credentials not configured. "
            "Set GOOGLE_CHAT_CLIENT_ID and GOOGLE_CHAT_CLIENT_SECRET."
        )

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_CHAT_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange authorization code for access and refresh tokens."""
    data = {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    response = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=30)
    response.raise_for_status()
    token_data = response.json()

    if "error" in token_data:
        error = token_data.get("error_description", token_data["error"])
        logger.error(f"Google Chat OAuth token exchange failed: {error}")
        raise ValueError(f"Google Chat OAuth failed: {error}")

    logger.info("Successfully exchanged Google Chat OAuth code for token")
    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh an expired access token."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=30)
    response.raise_for_status()
    token_data = response.json()

    if "error" in token_data:
        error = token_data.get("error_description", token_data["error"])
        logger.error(f"Google Chat token refresh failed: {error}")
        raise ValueError(f"Google Chat token refresh failed: {error}")

    return token_data


def get_user_info(access_token: str) -> Dict[str, Any]:
    """Fetch Google user profile info using the access token."""
    response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()
