"""PagerDuty OAuth2 utilities."""

import os
import logging
import requests
import urllib.parse
from time import time
from typing import Dict, Optional, Tuple
from utils.flags.feature_flags import is_pagerduty_oauth_enabled

logger = logging.getLogger(__name__)

PAGERDUTY_AUTH_URL = "https://app.pagerduty.com/oauth/authorize"
PAGERDUTY_TOKEN_URL = "https://app.pagerduty.com/oauth/token"
DEFAULT_SCOPES = "openid users.read incidents.read incidents.write services.read"

# Only load OAuth credentials if feature flag is enabled
if is_pagerduty_oauth_enabled():
    PAGERDUTY_CLIENT_ID = os.getenv("PAGERDUTY_CLIENT_ID")
    PAGERDUTY_CLIENT_SECRET = os.getenv("PAGERDUTY_CLIENT_SECRET")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL").rstrip("/")
    PAGERDUTY_REDIRECT_URI = f"{backend_url}/pagerduty/oauth/callback"
    
    if not PAGERDUTY_CLIENT_ID or not PAGERDUTY_CLIENT_SECRET or not PAGERDUTY_REDIRECT_URI:
        raise ValueError("PagerDuty OAuth credentials not configured. Please set PAGERDUTY_CLIENT_ID, PAGERDUTY_CLIENT_SECRET, and NEXT_PUBLIC_BACKEND_URL environment variables.")
else:
    PAGERDUTY_CLIENT_ID = None
    PAGERDUTY_CLIENT_SECRET = None
    PAGERDUTY_REDIRECT_URI = None


def get_auth_url(state: Optional[str] = None) -> str:
    """Generate PagerDuty OAuth URL."""
    if not is_pagerduty_oauth_enabled():
        raise ValueError("PagerDuty OAuth is not enabled")
    
    
    params = {
        "response_type": "code",
        "client_id": PAGERDUTY_CLIENT_ID,
        "redirect_uri": PAGERDUTY_REDIRECT_URI,
        "scope": DEFAULT_SCOPES,
    }
    if state:
        params["state"] = state
    
    return f"{PAGERDUTY_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _oauth_request(grant_data: Dict) -> Optional[Dict]:
    """Make OAuth token request."""
    try:
        response = requests.post(PAGERDUTY_TOKEN_URL, data=grant_data, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"[PAGERDUTY_OAUTH] Request failed: {e}")
        return None


def exchange_code_for_token(code: str) -> Optional[Dict]:
    """Exchange authorization code for tokens."""
    if not is_pagerduty_oauth_enabled():
        logger.error("[PAGERDUTY_OAUTH] OAuth is not enabled")
        return None

    
    return _oauth_request({
        "grant_type": "authorization_code",
        "client_id": PAGERDUTY_CLIENT_ID,
        "client_secret": PAGERDUTY_CLIENT_SECRET,
        "redirect_uri": PAGERDUTY_REDIRECT_URI,
        "code": code,
    })


def refresh_token_if_needed(token_data: Dict) -> Tuple[bool, Optional[Dict]]:
    """Refresh OAuth token if expired."""
    if not is_pagerduty_oauth_enabled():
        return False, None
    
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return False, None
    
    # Check if still valid (> 5 min remaining)
    if token_data.get("expires_at", 0) > int(time()) + 300:
        return True, token_data
    
    if not all([PAGERDUTY_CLIENT_ID, PAGERDUTY_CLIENT_SECRET]):
        return False, None
    
    new_tokens = _oauth_request({
        "grant_type": "refresh_token",
        "client_id": PAGERDUTY_CLIENT_ID,
        "client_secret": PAGERDUTY_CLIENT_SECRET,
        "refresh_token": refresh_token,
    })
    
    if not new_tokens:
        return False, None
    
    return True, {
        "access_token": new_tokens["access_token"],
        "expires_at": int(time()) + new_tokens.get("expires_in", 3600),
        "refresh_token": new_tokens.get("refresh_token", refresh_token),
    }

