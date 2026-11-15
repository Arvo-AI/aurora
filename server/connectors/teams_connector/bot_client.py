"""Post to Teams as the Aurora bot (Bot Framework), not as a delegated user."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

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


# Teams conversation ids look like 19:abc@thread.tacv2;messageid=1700000000000.
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_.@:;=-]+$")
# The bot's bearer token is only ever sent to Microsoft's Bot Connector hosts.
_SERVICE_URL_HOST_SUFFIXES = (".botframework.com",)
_SERVICE_URL_HOSTS = ("smba.trafficmanager.net", "botframework.com")


def is_allowed_service_url(url: Optional[str]) -> bool:
    """True only for https Bot Connector URLs, so a forged serviceUrl can't capture our token."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        # Port access raises on malformed values; only the default https port is valid
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    return host in _SERVICE_URL_HOSTS or host.endswith(_SERVICE_URL_HOST_SUFFIXES)


def _safe_conversation_id(value: str, *, label: str = "conversation_id") -> str:
    if not value or len(value) > 512 or value in (".", "..") or not _CONVERSATION_ID_RE.match(value):
        raise TeamsBotError(f"Invalid Bot Framework {label}")
    return value


def thread_conversation_id(channel_id: str, root_message_id: str) -> str:
    """Conversation id that posts into the thread under ``root_message_id``."""
    base = (channel_id or "").split(";", 1)[0]
    TeamsClient._safe_segment(base, label="channel_id")
    TeamsClient._safe_segment(root_message_id, label="message_id")
    return f"{base};messageid={root_message_id}"


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
    # Every outbound call carries the bot token, so refuse hosts we don't recognise
    if not is_allowed_service_url(url):
        raise TeamsBotError("Refusing to call a non-Bot-Connector service URL")
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
    *,
    timeout: int = 30,
) -> Dict[str, Any]:
    token = get_bot_access_token()
    url = f"{_normalized_service_url(service_url)}{path.lstrip('/')}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if not resp.ok:
        raise TeamsBotError(
            f"Bot Framework POST failed ({resp.status_code}) on {path}",
            status_code=resp.status_code,
        )
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def team_has_bot(service_url: Optional[str], team_thread_id: str) -> Optional[bool]:
    """True/False if the bot is/isn't installed in the team; None if Microsoft couldn't say."""
    team_thread_id = _safe_conversation_id(team_thread_id, label="team_id")
    url = f"{_normalized_service_url(service_url)}v3/teams/{team_thread_id}/conversations"
    resp = requests.get(url, headers={"Authorization": f"Bearer {get_bot_access_token()}"}, timeout=10)
    # The channel list is only served to a bot that is in the team
    if resp.ok:
        return True
    # Roster/permission refusal means the bot isn't a member
    if resp.status_code in (403, 404):
        return False
    # Auth or 5xx trouble says nothing about installation
    return None


def send_reply_in_conversation(
    *,
    service_url: str,
    conversation_id: str,
    text: str,
    reply_to_id: Optional[str] = None,
    text_format: str = "plain",
    timeout: int = 30,
) -> Dict[str, Any]:
    """Reply in an existing conversation (e.g. after an @mention activity)."""
    conversation_id = _safe_conversation_id(conversation_id)
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
        timeout=timeout,
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
    conversation_id = _safe_conversation_id(conversation_id)
    activity_id = _safe_conversation_id(activity_id, label="activity_id")
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
    # Mention-derived ids can carry a ;messageid= suffix that isn't part of the channel id
    channel_id = (channel_id or "").split(";", 1)[0]
    TeamsClient._safe_segment(tenant_id, label="tenant_id")
    TeamsClient._safe_segment(team_id, label="team_id")
    TeamsClient._safe_segment(channel_id, label="channel_id")

    svc = _normalized_service_url(service_url)
    # Create Conversation ignores replyToId, so a thread reply is posted into
    # the root message's own conversation id instead.
    if reply_to_id:
        return send_reply_in_conversation(
            service_url=svc,
            conversation_id=thread_conversation_id(channel_id, reply_to_id),
            text=text,
            text_format=text_format,
        )

    app_id = _app_id()
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
    return _post_activity(svc, "v3/conversations", body)


def tenant_id_for_aurora_user(user_id: str) -> Optional[str]:
    try:
        from utils.auth.stateless_auth import get_credentials_from_db

        creds = get_credentials_from_db(user_id, "teams") or {}
        return creds.get("tenant_id")
    except Exception:
        logger.debug("Could not resolve Teams tenant for user", exc_info=True)
        return None
