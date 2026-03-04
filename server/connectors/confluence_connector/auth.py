"""Authentication helpers for the Confluence connector.

Delegates to the shared atlassian_auth module for OAuth operations while
preserving the existing Confluence-specific public API.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from connectors.atlassian_auth.auth import (
    exchange_code_for_token as _exchange,
    fetch_accessible_resources,
    get_atlassian_oauth_config,
    get_auth_url as _get_auth_url,
    refresh_access_token,
    select_resource_for_product,
)

logger = logging.getLogger(__name__)

load_dotenv()

CONFLUENCE_AUTH_URL = "https://auth.atlassian.com/authorize"
CONFLUENCE_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
CONFLUENCE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

CONFLUENCE_SCOPES = "read:page:confluence read:space:confluence read:user:confluence search:confluence offline_access"
CONFLUENCE_AUDIENCE = "api.atlassian.com"

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
REDIRECT_URI = f"{FRONTEND_URL}/confluence/callback"


def _get_oauth_config() -> Dict[str, str]:
    config = get_atlassian_oauth_config(redirect_uri=REDIRECT_URI)
    config["scopes"] = CONFLUENCE_SCOPES
    return config


def _validate_oauth_config() -> Dict[str, str]:
    config = _get_oauth_config()
    missing = [key for key in ("client_id", "client_secret") if not config[key]]
    if missing:
        raise ValueError(
            f"Confluence OAuth configuration missing: {', '.join(missing)}"
        )
    return config


def get_auth_url(state: str) -> str:
    """Generate the Atlassian OAuth 2.0 authorization URL for Confluence."""
    return _get_auth_url(state, products=["confluence"], redirect_uri=REDIRECT_URI)


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens."""
    return _exchange(code, redirect_uri=REDIRECT_URI)


def select_confluence_resource(
    resources: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Pick a Confluence resource from accessible resources."""
    return select_resource_for_product(resources, "confluence")
