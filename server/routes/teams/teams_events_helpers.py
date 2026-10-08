"""Helpers for Microsoft Teams Bot Framework events."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import jwt
import requests
from flask import request as flask_request

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)
TITLE_MAX_LENGTH = 50

_OPENID_CONFIG: Dict[str, Any] | None = None
_BOTFRAMEWORK_JWKS_CLIENT: jwt.PyJWKClient | None = None
_BOTFRAMEWORK_ISSUER = "https://api.botframework.com"


def _botframework_jwks_client() -> jwt.PyJWKClient:
    global _OPENID_CONFIG, _BOTFRAMEWORK_JWKS_CLIENT
    if _OPENID_CONFIG is None:
        resp = requests.get(
            "https://login.botframework.com/v1/.well-known/openidconfiguration",
            timeout=15,
        )
        resp.raise_for_status()
        _OPENID_CONFIG = resp.json()
    if _BOTFRAMEWORK_JWKS_CLIENT is None:
        _BOTFRAMEWORK_JWKS_CLIENT = jwt.PyJWKClient(_OPENID_CONFIG["jwks_uri"])
    return _BOTFRAMEWORK_JWKS_CLIENT


def verify_teams_request() -> bool:
    """Validate the Bot Framework JWT on incoming activities."""
    auth_header = flask_request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    app_id = os.getenv("TEAMS_APP_ID") or os.getenv("TEAMS_CLIENT_ID")
    if not app_id:
        logger.error("TEAMS_APP_ID not set — cannot verify Teams bot requests")
        return False
    token = auth_header[7:]
    try:
        jwks_client = _botframework_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=_BOTFRAMEWORK_ISSUER,
        )
        return True
    except Exception as e:
        logger.warning("Teams JWT verification failed: %s", e)
        return False


def is_bot_mentioned(activity: Dict[str, Any]) -> bool:
    """True when the Teams activity @mentions this bot (Slack-style trigger)."""
    app_id = os.getenv("TEAMS_APP_ID") or os.getenv("TEAMS_CLIENT_ID") or ""
    if not app_id:
        return False
    bot_id = (activity.get("recipient") or {}).get("id") or ""
    bot_id_alt = f"28:{app_id}" if not app_id.startswith("28:") else app_id
    for ent in activity.get("entities") or []:
        if ent.get("type") != "mention":
            continue
        mentioned = ent.get("mentioned") or {}
        mid = mentioned.get("id") or ""
        if mid == bot_id or mid == bot_id_alt or app_id in mid:
            return True
    return False


def get_user_id_from_teams_tenant(tenant_id: str) -> Optional[str]:
    if not tenant_id:
        return None
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT user_id FROM user_tokens
                   WHERE provider = 'teams' AND subscription_id = %s AND is_active = TRUE
                   ORDER BY timestamp DESC LIMIT 1""",
                (tenant_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception("Failed to resolve Teams tenant to Aurora user")
        return None


def send_message_to_aurora(
    user_id: str,
    message_text: str,
    team_id: str,
    channel_id: str,
    reply_to_id: str | None = None,
    service_url: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
):
    from chat.background.task import create_background_chat_session, run_background_chat

    if not session_id:
        title = "Teams: " + (
            message_text[:TITLE_MAX_LENGTH] + "..."
            if len(message_text) > TITLE_MAX_LENGTH
            else message_text
        )
        trigger_metadata = {
            "source": "teams",
            "team_id": team_id,
            "channel_id": channel_id,
            "reply_to_id": reply_to_id,
            "service_url": service_url,
            "conversation_id": conversation_id,
        }
        session_id = create_background_chat_session(
            user_id=user_id,
            title=title,
            trigger_metadata=trigger_metadata,
        )

    prompt = message_text
    run_background_chat.delay(
        user_id=user_id,
        session_id=session_id,
        initial_message=prompt,
        trigger_metadata={
            "source": "teams",
            "team_id": team_id,
            "channel_id": channel_id,
            "reply_to_id": reply_to_id,
            "service_url": service_url,
            "conversation_id": conversation_id,
        },
        send_notifications=False,
        mode="ask",
    )
