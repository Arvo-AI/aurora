"""
Google Chat OAuth 2.0 for the one-time setup flow.

User OAuth is used ONLY to create/find the incidents space inside the
customer's Google Workspace.  All ongoing messaging goes through the Chat
app's service account (see client.py).
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

GOOGLE_CHAT_SETUP_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.memberships",
    "https://www.googleapis.com/auth/chat.memberships.app",
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
        "scope": " ".join(GOOGLE_CHAT_SETUP_SCOPES),
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
        error_type = token_data["error"]
        logger.error("Google Chat OAuth token exchange failed: %s", error_type)
        raise ValueError(f"Google Chat OAuth failed: {error_type}")

    logger.info("Successfully exchanged Google Chat OAuth code for token")
    return token_data
