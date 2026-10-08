"""Microsoft Entra ID OAuth for the Teams connector (delegated Graph access)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Delegated scopes for teammate behaviour (list channels, read/post messages).
TEAMS_SCOPES = (
    "Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Read.All "
    "Chat.Read offline_access openid profile"
)


def _redirect_base() -> str:
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if ngrok_url and backend_url.startswith("http://localhost"):
        return ngrok_url
    return backend_url


def _config() -> Dict[str, str]:
    tenant = os.getenv("TEAMS_TENANT_ID", "common")
    return {
        "client_id": os.getenv("TEAMS_CLIENT_ID", ""),
        "client_secret": os.getenv("TEAMS_CLIENT_SECRET", ""),
        "tenant_id": tenant,
        "redirect_uri": f"{_redirect_base()}/teams/callback",
        "scopes": TEAMS_SCOPES,
    }


def _validate() -> Dict[str, str]:
    cfg = _config()
    missing_env: list[str] = []
    if not cfg["client_id"]:
        missing_env.append("TEAMS_CLIENT_ID")
    if not cfg["client_secret"]:
        missing_env.append("TEAMS_CLIENT_SECRET")
    if not _redirect_base():
        missing_env.append("NEXT_PUBLIC_BACKEND_URL (or NGROK_URL for local dev)")
    if missing_env:
        raise ValueError(
            "Microsoft Teams OAuth is not configured on this Aurora instance. Set "
            + ", ".join(missing_env)
            + " in .env and restart the server. Setup guide: "
            "https://arvo-ai.github.io/aurora/docs/integrations/connectors#microsoft-teams"
        )
    return cfg


def get_auth_url(state: str) -> str:
    if not state:
        raise ValueError("State parameter is required for Teams OAuth.")
    cfg = _validate()
    params = {
        "client_id": cfg["client_id"],
        "scope": cfg["scopes"],
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTH_URL.format(tenant=cfg['tenant_id'])}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> Dict[str, Any]:
    if not code:
        raise ValueError("Authorization code is required")
    cfg = _validate()
    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
        "scope": cfg["scopes"],
    }
    response = requests.post(TOKEN_URL.format(tenant=cfg["tenant_id"]), data=payload, timeout=30)
    if not response.ok:
        logger.error("Teams OAuth token exchange failed: status=%s", response.status_code)
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    if not refresh_token:
        raise ValueError("refresh_token is required")
    cfg = _validate()
    payload = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": refresh_token,
        "scope": cfg["scopes"],
    }
    response = requests.post(TOKEN_URL.format(tenant=cfg["tenant_id"]), data=payload, timeout=30)
    if not response.ok:
        logger.error("Teams OAuth token refresh failed: status=%s", response.status_code)
    response.raise_for_status()
    return response.json()
