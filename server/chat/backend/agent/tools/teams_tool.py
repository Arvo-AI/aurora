"""Microsoft Teams tools for the agent (read history, list connected channels, post)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from connectors.teams_connector.client import TeamsAPIError, get_teams_client_for_user

logger = logging.getLogger(__name__)

_ERR_NO_USER = "No user context available."
_ERR_NOT_CONNECTED = "Microsoft Teams not connected for this user."
_MAX_POST_CHARS = 3000
_PROVIDER = "teams"


def is_teams_connected(user_id: str) -> bool:
    try:
        return get_teams_client_for_user(user_id) is not None
    except Exception:
        return False


def _format_message(msg: dict) -> dict:
    created = msg.get("createdDateTime") or ""
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        time_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        time_str = created
    body = (msg.get("body") or {}).get("content") or ""
    return {
        "id": msg.get("id"),
        "time": time_str,
        "from": ((msg.get("from") or {}).get("user") or {}).get("displayName", "unknown"),
        "text": body,
        "reply_to_id": msg.get("replyToId"),
    }


class GetTeamsChannelHistoryArgs(BaseModel):
    team_id: str = Field(description="Microsoft Teams team ID (GUID)")
    channel_id: str = Field(description="Teams channel ID")
    limit: int = Field(default=50, description="Maximum messages (1-50)")


class PostTeamsMessageArgs(BaseModel):
    team_id: str = Field(description="Microsoft Teams team ID (GUID)")
    channel_id: str = Field(description="Teams channel ID to post to")
    text: str = Field(description="Plain-text message body")
    reply_to_id: Optional[str] = Field(
        default=None,
        description="Message ID to reply under (thread follow-up). Omit for a new top-level message.",
    )


class GetConnectedTeamsChannelsArgs(BaseModel):
    pass


def get_teams_channel_history(
    team_id: str,
    channel_id: str,
    limit: int = 50,
    user_id: str | None = None,
    **kwargs,
) -> str:
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    if not team_id or not channel_id:
        return json.dumps({"error": "team_id and channel_id are required."})
    client = get_teams_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})
    limit = max(1, min(limit, 50))
    try:
        messages = client.list_channel_messages(team_id, channel_id, limit=limit)
        formatted = [_format_message(m) for m in messages]
        return json.dumps({
            "status": "ok",
            "team_id": team_id,
            "channel_id": channel_id,
            "messages": formatted,
            "count": len(formatted),
        })
    except TeamsAPIError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.info("[TeamsTool] Failed to read channel history")
        return json.dumps({"error": f"Failed to fetch channel history: {e}"})


def post_teams_message(
    team_id: str,
    channel_id: str,
    text: str,
    reply_to_id: Optional[str] = None,
    user_id: str | None = None,
    **kwargs,
) -> str:
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    if not team_id or not channel_id:
        return json.dumps({"error": "team_id and channel_id are required."})
    text = (text or "").strip()
    if not text:
        return json.dumps({"error": "text is required and must be non-empty."})
    if len(text) > _MAX_POST_CHARS:
        text = text[: _MAX_POST_CHARS - 3] + "..."

    from services.channels import registry

    is_member = registry.channel_membership(user_id, _PROVIDER, channel_id)
    if is_member is None:
        return json.dumps({
            "error": f"Could not verify Aurora's membership of channel {channel_id}; message not posted.",
            "code": "membership_check_failed",
        })
    if not is_member:
        return json.dumps({
            "error": (
                f"Aurora is not registered as a member of channel {channel_id}. "
                "Only post to channels returned by get_connected_teams_channels."
            ),
            "code": "channel_not_active",
        })

    client = get_teams_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})

    try:
        response = client.send_channel_message(team_id, channel_id, text, reply_to_id=reply_to_id or None)
    except TeamsAPIError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.info("[TeamsTool] Failed to post message")
        return json.dumps({"error": f"Failed to post message: {e}"})

    message_id = response.get("id")
    return json.dumps({
        "status": "posted",
        "team_id": team_id,
        "channel_id": channel_id,
        "id": message_id,
        "threaded": bool(reply_to_id),
    })


def list_teams_channels(user_id: str | None = None, **kwargs) -> str:
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    client = get_teams_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})
    try:
        from services.channels import prefs as channel_prefs

        hidden = channel_prefs.get_hidden_channel_ids(user_id, _PROVIDER)
        result = []
        for team in client.list_joined_teams():
            team_id = team.get("id")
            team_name = team.get("displayName") or "team"
            if not team_id:
                continue
            for ch in client.list_team_channels(team_id):
                cid = ch.get("id")
                if not cid or cid in hidden:
                    continue
                result.append({
                    "id": cid,
                    "team_id": team_id,
                    "name": ch.get("displayName"),
                    "description": ch.get("description") or "",
                    "team_name": team_name,
                    "is_private": ch.get("membershipType") == "private",
                })
                if len(result) >= 100:
                    break
            if len(result) >= 100:
                break
        return json.dumps({"status": "ok", "channels": result, "total": len(result)})
    except TeamsAPIError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to list Teams channels: {e}"})


class GetTeamsThreadRepliesArgs(BaseModel):
    team_id: str = Field(description="Microsoft Teams team ID (GUID)")
    channel_id: str = Field(description="Teams channel ID")
    message_id: str = Field(description="Parent message ID")
    limit: int = Field(default=50, description="Maximum replies (1-50)")


def get_teams_thread_replies(
    team_id: str,
    channel_id: str,
    message_id: str,
    limit: int = 50,
    user_id: str | None = None,
    **kwargs,
) -> str:
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    client = get_teams_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})
    limit = max(1, min(limit, 50))
    try:
        replies = client.list_message_replies(team_id, channel_id, message_id, limit=limit)
        formatted = [_format_message(m) for m in replies]
        return json.dumps({
            "status": "ok",
            "team_id": team_id,
            "channel_id": channel_id,
            "message_id": message_id,
            "replies": formatted,
            "count": len(formatted),
        })
    except TeamsAPIError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch thread replies: {e}"})


def get_connected_teams_channels(user_id: str | None = None, **kwargs) -> str:
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    try:
        from services.channels import registry

        channels = registry.get_connected_channels(user_id, _PROVIDER)
        if not channels:
            return json.dumps({
                "channels": [],
                "message": (
                    "No active Microsoft Teams channels yet. Activate channels on the "
                    "Teams manage page (or add the Aurora app to a team channel and refresh)."
                ),
            })
        return json.dumps({"channels": channels})
    except Exception as e:
        logger.exception("Error fetching connected Teams channels")
        return json.dumps({"error": f"Failed to fetch connected Teams channels: {e}"})
