"""Shared Atlassian OAuth 2.0 authentication module.

Product-agnostic OAuth that assembles scopes dynamically based on which
Atlassian products the user wants to connect (Confluence, Jira, or both).
"""

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

ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
ATLASSIAN_AUDIENCE = "api.atlassian.com"

CONFLUENCE_SCOPES = "read:page:confluence read:space:confluence read:user:confluence search:confluence"
JIRA_SCOPES = "read:jira-work write:jira-work read:jira-user"
COMMON_SCOPES = "offline_access"

PRODUCT_SCOPES: Dict[str, str] = {
    "confluence": CONFLUENCE_SCOPES,
    "jira": JIRA_SCOPES,
}

FRONTEND_URL = os.getenv("FRONTEND_URL", "")


def get_atlassian_oauth_config(redirect_uri: Optional[str] = None) -> Dict[str, str]:
    """Read Atlassian OAuth app credentials from environment."""
    client_id = os.getenv("ATLASSIAN_CLIENT_ID", "")
    client_secret = os.getenv("ATLASSIAN_CLIENT_SECRET", "")
    if redirect_uri is None:
        redirect_uri = f"{FRONTEND_URL}/atlassian/callback"
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "audience": ATLASSIAN_AUDIENCE,
    }


def _validate_config(config: Dict[str, str]) -> Dict[str, str]:
    missing = [k for k in ("client_id", "client_secret") if not config.get(k)]
    if missing:
        raise ValueError(f"Atlassian OAuth configuration missing: {', '.join(missing)}")
    return config


def build_scopes(products: List[str]) -> str:
    """Assemble OAuth scopes from a list of product names."""
    parts: List[str] = []
    for product in products:
        scope_str = PRODUCT_SCOPES.get(product)
        if scope_str:
            parts.append(scope_str)
    parts.append(COMMON_SCOPES)
    return " ".join(parts)


def get_auth_url(state: str, products: Optional[List[str]] = None,
                 redirect_uri: Optional[str] = None) -> str:
    """Generate the Atlassian OAuth 2.0 authorization URL with dynamic scopes."""
    if not state:
        raise ValueError("State parameter is required for Atlassian OAuth.")

    if not products:
        products = ["confluence"]

    config = _validate_config(get_atlassian_oauth_config(redirect_uri))
    scopes = build_scopes(products)
    params = {
        "audience": config["audience"],
        "client_id": config["client_id"],
        "scope": scopes,
        "redirect_uri": redirect_uri or config["redirect_uri"],
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{ATLASSIAN_AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens."""
    config = _validate_config(get_atlassian_oauth_config(redirect_uri))
    payload = {
        "grant_type": "authorization_code",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri or config["redirect_uri"],
    }

    response = requests.post(ATLASSIAN_TOKEN_URL, json=payload, timeout=30)
    if not response.ok:
        logger.error(
            "Atlassian OAuth token exchange failed (%s)",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    if not token_data.get("access_token"):
        logger.error(
            "Atlassian OAuth response missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("Atlassian OAuth failed: missing access_token")

    return token_data


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh an Atlassian OAuth access token."""
    if not refresh_token:
        raise ValueError("refresh_token is required")

    config = _validate_config(get_atlassian_oauth_config())
    payload = {
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": refresh_token,
    }

    response = requests.post(ATLASSIAN_TOKEN_URL, json=payload, timeout=30)
    if not response.ok:
        logger.error(
            "Atlassian OAuth refresh failed (%s)",
            response.status_code,
        )
    response.raise_for_status()
    token_data = response.json()

    access_token = token_data.get("access_token")
    if not access_token:
        logger.error(
            "Atlassian OAuth refresh missing access_token. Keys: %s",
            list(token_data.keys()),
        )
        raise ValueError("Atlassian OAuth refresh failed: missing access_token")

    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            token_data["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            logger.debug("Unable to compute expires_at for Atlassian refresh response.")

    return token_data


def fetch_accessible_resources(access_token: str) -> List[Dict[str, Any]]:
    """Fetch Atlassian cloud sites accessible by the OAuth token."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        response = requests.get(ATLASSIAN_RESOURCES_URL, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("Atlassian accessible-resources request failed (status=%s): %s", status, type(exc).__name__)
        raise


def select_resource_for_product(
    resources: List[Dict[str, Any]],
    product: str,
) -> Optional[Dict[str, Any]]:
    """Pick a resource from accessible-resources that has a scope matching *product*.

    Falls back to the first resource if none explicitly match.
    """
    for resource in resources:
        scopes = resource.get("scopes") or []
        if any(product in scope for scope in scopes):
            return resource
    return resources[0] if resources else None
