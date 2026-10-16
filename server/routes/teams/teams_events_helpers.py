"""Helpers for Microsoft Teams Bot Framework events."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import jwt
import requests
from flask import request as flask_request

from connectors.teams_connector.bot_client import is_allowed_service_url
from utils.cache.redis_client import get_redis_client
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)
TITLE_MAX_LENGTH = 50
# Bot Framework channel messages allow ~28k; keep a sane cap for readability.
TEAMS_MAX_MESSAGE_LENGTH = 28_000


def format_response_for_teams(text: str, max_length: int = TEAMS_MAX_MESSAGE_LENGTH) -> str:
    """Plain text for Teams (no Slack mrkdwn). Reuses RCA note markdown stripping."""
    from utils.notifications.rca_note import to_plain_text

    if not text:
        return ""
    return to_plain_text(text, max_chars=max_length)

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


def verify_teams_request_claims() -> Optional[Dict[str, Any]]:
    """Validate the Bot Framework JWT on incoming activities; returns its claims or None."""
    auth_header = flask_request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    app_id = os.getenv("TEAMS_APP_ID") or os.getenv("TEAMS_CLIENT_ID")
    if not app_id:
        logger.error("TEAMS_APP_ID not set — cannot verify Teams bot requests")
        return None
    token = auth_header[7:]
    try:
        jwks_client = _botframework_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=_BOTFRAMEWORK_ISSUER,
        )
    except Exception as e:
        logger.warning("Teams JWT verification failed: %s", sanitize(e))
        return None


def verify_teams_request() -> bool:
    return verify_teams_request_claims() is not None


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def is_trusted_service_url(service_url: Optional[str], claims: Dict[str, Any]) -> bool:
    """The body's serviceUrl must be a Bot Connector host and match the signed token's claim."""
    if not is_allowed_service_url(service_url):
        return False
    claimed = claims.get("serviceurl")
    # Claim present but different — the body was tampered with or the token replayed
    if claimed and _normalize_url(str(claimed)) != _normalize_url(str(service_url)):
        return False
    return True


ACTIVITY_DEDUPE_TTL_SECONDS = 3600


def claim_activity_once(activity_id: Optional[str]) -> bool:
    """True the first time an activity id is seen; Bot Framework retries slow POSTs."""
    # No id to key on — nothing to dedupe
    if not activity_id:
        return True
    try:
        redis = get_redis_client()
        # Redis down — better a rare duplicate reply than dropping every message
        if not redis:
            return True
        return bool(redis.set(f"teams:activity:{activity_id}", "1", nx=True, ex=ACTIVITY_DEDUPE_TTL_SECONDS))
    except Exception:
        logger.warning("Teams activity dedupe unavailable; processing anyway", exc_info=True)
        return True


def strip_thread_suffix(conversation_id: Optional[str]) -> Optional[str]:
    """Drop the ``;messageid=`` thread suffix so only the channel id remains."""
    if not conversation_id:
        return None
    return conversation_id.split(";", 1)[0] or None


def extract_team_and_channel_ids(activity: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Graph-usable (team_id, channel_id) from a Teams activity; team is None in a DM."""
    channel_data = activity.get("channelData") or {}
    team = channel_data.get("team") or {}
    # aadGroupId is the Graph team GUID; team.id is a thread-style id Graph rejects
    team_id = team.get("aadGroupId") or team.get("id") or channel_data.get("teamsTeamId")
    conversation = activity.get("conversation") or {}
    raw_channel = (channel_data.get("channel") or {}).get("id") or conversation.get("id")
    return team_id, strip_thread_suffix(raw_channel)


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
        # FORCE RLS hides user_tokens without an org set, and an inbound activity has none
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT org_id FROM users WHERE org_id IS NOT NULL")
            org_ids = [row[0] for row in cur.fetchall()]
            for org_id in org_ids:
                cur.execute("SET myapp.current_org_id = %s", (org_id,))
                cur.execute(
                    """SELECT user_id FROM user_tokens
                       WHERE provider = 'teams' AND subscription_id = %s AND is_active = TRUE
                       ORDER BY timestamp DESC LIMIT 1""",
                    (tenant_id,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
            return None
    except Exception:
        logger.exception("Failed to resolve Teams tenant to Aurora user")
        return None


def _post_teams_thinking_placeholder(
    *,
    service_url: str | None,
    conversation_id: str | None,
    reply_to_id: str | None,
) -> str | None:
    if not service_url or not conversation_id:
        return None
    try:
        from connectors.teams_connector.bot_client import send_reply_in_conversation

        result = send_reply_in_conversation(
            service_url=service_url,
            conversation_id=conversation_id,
            text="Thinking…",
            reply_to_id=reply_to_id,
            # Short so a slow Teams call can't hold the webhook open until Bot Framework retries
            timeout=5,
        )
        return result.get("id") if isinstance(result, dict) else None
    except Exception:
        logger.warning("Failed to post Teams thinking placeholder", exc_info=True)
        return None


def send_message_to_aurora(
    user_id: str,
    message_text: str,
    team_id: str | None,
    channel_id: str,
    reply_to_id: str | None = None,
    service_url: str | None = None,
    conversation_id: str | None = None,
    session_id: str | None = None,
):
    from chat.background.task import create_background_chat_session, run_background_chat

    thinking_activity_id = _post_teams_thinking_placeholder(
        service_url=service_url,
        conversation_id=conversation_id,
        reply_to_id=reply_to_id,
    )

    trigger_metadata = {
        "source": "teams",
        "team_id": team_id,
        "channel_id": channel_id,
        "reply_to_id": reply_to_id,
        "service_url": service_url,
        "conversation_id": conversation_id,
        "thinking_activity_id": thinking_activity_id,
    }

    # Caller already has a session — only new conversations need one created
    if not session_id:
        title = "Teams: " + (
            message_text[:TITLE_MAX_LENGTH] + "..."
            if len(message_text) > TITLE_MAX_LENGTH
            else message_text
        )
        session_id = create_background_chat_session(
            user_id=user_id,
            title=title,
            trigger_metadata=trigger_metadata,
        )

    run_background_chat.delay(
        user_id=user_id,
        session_id=session_id,
        initial_message=message_text,
        trigger_metadata=trigger_metadata,
        send_notifications=False,
        mode="ask",
    )
