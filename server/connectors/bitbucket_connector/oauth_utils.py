"""
Bitbucket Cloud OAuth 2.0 utilities.
Handles authorization URL generation, token exchange, and token refresh.
"""
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

BITBUCKET_AUTHORIZE_URL = "https://bitbucket.org/site/oauth2/authorize"
BITBUCKET_TOKEN_URL = "https://bitbucket.org/site/oauth2/access_token"
BITBUCKET_SCOPES = "repository:write pullrequest:write issue:write account project pipeline:write"
REQUEST_TIMEOUT = 30  # seconds


def _get_redirect_uri():
    """Build the OAuth redirect URI from the backend URL."""
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    return f"{backend_url}/bitbucket/callback"


def generate_oauth_state(user_id):
    """
    Generate a cryptographic state token for the OAuth flow and store it in Redis.

    Returns:
        A random state token string (the user_id is stored server-side, not in the URL).
    """
    from utils.auth.oauth2_state_cache import store_oauth2_state

    state = secrets.token_urlsafe(32)
    store_oauth2_state(state=state, user_id=user_id, endpoint="bitbucket")
    return state


def validate_oauth_state(state):
    """
    Validate a state token from the OAuth callback and return the associated user_id.

    Returns:
        The user_id if valid, or None if the token is invalid/expired/replayed.
    """
    from utils.auth.oauth2_state_cache import retrieve_oauth2_state

    data = retrieve_oauth2_state(state)
    if not data:
        logger.warning("Invalid or expired Bitbucket OAuth state token")
        return None
    return data.get("user_id")


def get_auth_url(user_id):
    """
    Build the Bitbucket OAuth authorization URL with CSRF-safe state.

    Args:
        user_id: The user initiating the OAuth flow.

    Returns:
        The full authorization URL string.
    """
    client_id = os.getenv("BB_OAUTH_CLIENT_ID")
    redirect_uri = _get_redirect_uri()
    state = generate_oauth_state(user_id)

    params = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": BITBUCKET_SCOPES,
        "state": state,
        "redirect_uri": redirect_uri,
    })
    return f"{BITBUCKET_AUTHORIZE_URL}?{params}"


def exchange_code_for_token(code):
    """
    Exchange an authorization code for access and refresh tokens.

    Args:
        code: The authorization code from the OAuth callback.

    Returns:
        The token response JSON dict, or None on failure.
    """
    client_id = os.getenv("BB_OAUTH_CLIENT_ID")
    client_secret = os.getenv("BB_OAUTH_CLIENT_SECRET")
    redirect_uri = _get_redirect_uri()

    try:
        response = requests.post(
            BITBUCKET_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            logger.error(f"Bitbucket token exchange failed: {response.status_code} {response.text}")
            return None

        return response.json()
    except Exception as e:
        logger.error(f"Error exchanging Bitbucket code for token: {e}", exc_info=True)
        return None


def refresh_access_token(refresh_token):
    """
    Refresh an expired Bitbucket access token.

    Args:
        refresh_token: The refresh token from a previous token exchange.

    Returns:
        The new token response JSON dict, or None on failure.
    """
    client_id = os.getenv("BB_OAUTH_CLIENT_ID")
    client_secret = os.getenv("BB_OAUTH_CLIENT_SECRET")

    try:
        response = requests.post(
            BITBUCKET_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            logger.error(f"Bitbucket token refresh failed: {response.status_code} {response.text}")
            return None

        return response.json()
    except Exception as e:
        logger.error(f"Error refreshing Bitbucket token: {e}", exc_info=True)
        return None


def refresh_token_if_needed(token_data):
    """
    Check if the access token is expired (or within 5-min buffer) and refresh if needed.

    Args:
        token_data: Dict containing at least ``access_token``, ``refresh_token``,
                    and ``expires_at`` (epoch seconds).

    Returns:
        Updated token_data dict (may contain new access_token / expires_at),
        or the original dict if no refresh was needed.
    """
    expires_at = token_data.get("expires_at", 0)
    buffer_seconds = 300  # 5 minutes

    if time.time() + buffer_seconds < expires_at:
        # Token still valid
        return token_data

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        logger.warning("Bitbucket token expired but no refresh_token available")
        return token_data

    logger.info("Bitbucket access token expired or expiring soon, refreshing...")
    new_tokens = refresh_access_token(refresh_token)

    if not new_tokens:
        logger.error("Failed to refresh Bitbucket token")
        return token_data

    # Merge new values into token_data
    token_data["access_token"] = new_tokens["access_token"]
    token_data["refresh_token"] = new_tokens.get("refresh_token", refresh_token)
    token_data["expires_at"] = time.time() + new_tokens.get("expires_in", 7200)

    return token_data
