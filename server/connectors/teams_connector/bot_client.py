"""Post to Teams as the Aurora bot (Bot Framework), not as a delegated user."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

from connectors.teams_connector.client import TeamsAPIError, TeamsClient

logger = logging.getLogger(__name__)

# Default Teams Bot Framework endpoint when no inbound activity supplied one.
_DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
# Multi-tenant (legacy) bots use the botframework.com tenant; single-tenant bots must use
# their Entra directory — see Bot Connector authentication docs.
_TOKEN_URL_MULTITENANT = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
_TOKEN_SCOPE = "https://api.botframework.com/.default"
_TOKEN_CACHE: Dict[str, Any] = {"url": None, "token": None, "expires_at": 0.0}


class TeamsBotError(TeamsAPIError):
    pass


def _app_id() -> str:
    app_id = (os.getenv("TEAMS_APP_ID") or os.getenv("TEAMS_CLIENT_ID") or "").strip()
    if not app_id:
        raise TeamsBotError("TEAMS_APP_ID (or TEAMS_CLIENT_ID) is not configured")
    return app_id


def _app_password() -> str:
    secret = (os.getenv("TEAMS_CLIENT_SECRET") or "").strip()
    if not secret:
        raise TeamsBotError("TEAMS_CLIENT_SECRET is not configured")
    return secret


def _bot_token_url() -> str:
    tenant = (os.getenv("TEAMS_TENANT_ID") or "").strip()
    if tenant.lower() in ("", "common", "organizations", "consumers"):
        return _TOKEN_URL_MULTITENANT
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _bot_recipient_id(app_id: str) -> str:
    # Teams channel activities use the 28:{appId} recipient form.
    if app_id.startswith("28:"):
        return app_id
    return f"28:{app_id}"


def get_bot_access_token() -> str:
    now = time.time()
    token_url = _bot_token_url()
    cached = _TOKEN_CACHE.get("token")
    if (
        cached
        and _TOKEN_CACHE.get("url") == token_url
        and now < float(_TOKEN_CACHE.get("expires_at") or 0) - 60
    ):
        return cached

    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": _app_id(),
            "client_secret": _app_password(),
            "scope": _TOKEN_SCOPE,
        },
        timeout=30,
    )
    if not resp.ok:
        raise TeamsBotError(
            f"Bot Framework token request failed ({resp.status_code})",
            status_code=resp.status_code,
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise TeamsBotError("Bot Framework token response missing access_token")
    _TOKEN_CACHE["url"] = token_url
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + int(data.get("expires_in") or 3600)
    return token


def _normalized_service_url(service_url: Optional[str]) -> str:
    url = (service_url or _DEFAULT_SERVICE_URL).strip()
    if not url.endswith("/"):
        url += "/"
    return url


def _put_activity(
    service_url: str,
    path: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    token = get_bot_access_token()
    url = f"{_normalized_service_url(service_url)}{path.lstrip('/')}"
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if not resp.ok:
        raise TeamsBotError(
            f"Bot Framework PUT failed ({resp.status_code}) on {path}",
            status_code=resp.status_code,
        )
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _post_activity(
    service_url: str,
    path: str,
    body: Dict[str, Any],
) -> Dict[str, Any]:
    token = get_bot_access_token()
    url = f"{_normalized_service_url(service_url)}{path.lstrip('/')}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if not resp.ok:
        raise TeamsBotError(
            f"Bot Framework POST failed ({resp.status_code}) on {path}",
            status_code=resp.status_code,
        )
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def send_reply_in_conversation(
    *,
    service_url: str,
    conversation_id: str,
    text: str,
    reply_to_id: Optional[str] = None,
    text_format: str = "plain",
) -> Dict[str, Any]:
    """Reply in an existing conversation (e.g. after an @mention activity)."""
    app_id = _app_id()
    activity: Dict[str, Any] = {
        "type": "message",
        "from": {"id": _bot_recipient_id(app_id), "name": "Aurora"},
        "text": text,
        "textFormat": "html" if text_format == "html" else "plain",
    }
    if reply_to_id:
        activity["replyToId"] = reply_to_id
    return _post_activity(
        service_url,
        f"v3/conversations/{conversation_id}/activities",
        activity,
    )


def update_activity_in_conversation(
    *,
    service_url: str,
    conversation_id: str,
    activity_id: str,
    text: str,
    text_format: str = "plain",
) -> Dict[str, Any]:
    """Replace an existing bot message (e.g. swap 'Thinking…' for the final reply)."""
    app_id = _app_id()
    activity: Dict[str, Any] = {
        "type": "message",
        "id": activity_id,
        "from": {"id": _bot_recipient_id(app_id), "name": "Aurora"},
        "text": text,
        "textFormat": "html" if text_format == "html" else "plain",
    }
    return _put_activity(
        service_url,
        f"v3/conversations/{conversation_id}/activities/{activity_id}",
        activity,
    )


def send_message_to_team_channel(
    *,
    tenant_id: str,
    team_id: str,
    channel_id: str,
    text: str,
    reply_to_id: Optional[str] = None,
    service_url: Optional[str] = None,
    text_format: str = "plain",
) -> Dict[str, Any]:
    """Proactive channel message as the bot (incident cards, routing, tools)."""
    TeamsClient._safe_segment(tenant_id, label="tenant_id")
    TeamsClient._safe_segment(team_id, label="team_id")
    TeamsClient._safe_segment(channel_id, label="channel_id")

    app_id = _app_id()
    svc = _normalized_service_url(service_url)
    body: Dict[str, Any] = {
        "bot": {"id": _bot_recipient_id(app_id), "name": "Aurora"},
        "isGroup": True,
        "tenantId": tenant_id,
        "channelData": {
            "team": {"id": team_id},
            "channel": {"id": channel_id},
        },
        "activity": {
            "type": "message",
            "from": {"id": _bot_recipient_id(app_id), "name": "Aurora"},
            "text": text,
            "textFormat": "html" if text_format == "html" else "plain",
        },
    }
    if reply_to_id:
        body["activity"]["replyToId"] = reply_to_id
    return _post_activity(svc, "v3/conversations", body)


def tenant_id_for_aurora_user(user_id: str) -> Optional[str]:
    try:
        from utils.auth.stateless_auth import get_credentials_from_db

        creds = get_credentials_from_db(user_id, "teams") or {}
        return creds.get("tenant_id")
    except Exception:
        logger.debug("Could not resolve Teams tenant for user", exc_info=True)
        return None
