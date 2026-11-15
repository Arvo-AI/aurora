"""Teams conversationUpdate events: keep bot-install and channel state in sync."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from routes.teams import teams_channels, teams_installs
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_CHANNEL_ADDED_EVENTS = ("channelCreated", "channelRestored")


def _bot_ids() -> set[str]:
    app_id = (os.getenv("TEAMS_APP_ID") or os.getenv("TEAMS_CLIENT_ID") or "").strip()
    if not app_id:
        return set()
    return {app_id, app_id if app_id.startswith("28:") else f"28:{app_id}"}


def _includes_bot(members: Any) -> bool:
    bots = _bot_ids()
    return any(isinstance(m, dict) and m.get("id") in bots for m in members or [])


def handle_conversation_update(user_id: str, activity: Dict[str, Any]) -> None:
    channel_data = activity.get("channelData") or {}
    event = channel_data.get("eventType")
    # aadGroupId is the Graph team id; personal chats have no team to track
    team_id = (channel_data.get("team") or {}).get("aadGroupId")
    if not team_id:
        return

    # Bot removed, or the whole team deleted — nothing to post to anymore
    if event == "teamDeleted" or _includes_bot(activity.get("membersRemoved")):
        teams_installs.mark_team_uninstalled(user_id, team_id)
        teams_channels.remove_team_channels(user_id, team_id)
        return

    # Any other event reached us, so the bot is in this team
    teams_installs.mark_team_installed(user_id, team_id)

    channel = channel_data.get("channel") or {}
    channel_id = channel.get("id")

    # Bot just added — pull the team's channels in as active
    if _includes_bot(activity.get("membersAdded")):
        _enqueue(user_id, "sync")
        return

    # New channel in a team the bot is in
    if event in _CHANNEL_ADDED_EVENTS and channel_id:
        _enqueue(user_id, "channel", channel_id, team_id)
    # Channel gone in Teams — drop our copy
    elif event == "channelDeleted" and channel_id:
        teams_channels.remove_channel(user_id, channel_id)
    # Keep the stored name current
    elif event == "channelRenamed" and channel_id:
        teams_channels.rename_channel(user_id, channel_id, channel.get("name") or "")


def _enqueue(user_id: str, kind: str, channel_id: str | None = None, team_id: str | None = None) -> None:
    try:
        from routes.teams.teams_channel_metadata import auto_register_channels_task, register_channel_task

        if kind == "sync":
            auto_register_channels_task.delay(user_id)
        else:
            register_channel_task.delay(user_id, channel_id, team_id)
    except Exception as e:
        logger.warning("[TeamsLifecycle] could not enqueue %s: %s", kind, sanitize(e))
