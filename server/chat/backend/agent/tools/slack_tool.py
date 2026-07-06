"""
Slack Tools for Agent

Agent-callable tools for reading Slack channel messages and threads.
Used by the "Generate Postmortem" action to gather human context
from team conversations during incidents.

NOTE: A shared run_slack_tool() callback pattern was considered but rejected —
with only 3 tools, the indirection decreases readability for little benefit.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from connectors.slack_connector.client import get_slack_client_for_user

logger = logging.getLogger(__name__)

_ERR_NO_USER = "No user context available."
_ERR_NOT_CONNECTED = "Slack not connected for this user."


class ListSlackChannelsArgs(BaseModel):
    """Arguments for listing Slack channels."""
    pass


class GetChannelHistoryArgs(BaseModel):
    """Arguments for fetching channel message history."""
    channel_id: str = Field(description="The Slack channel ID (e.g. C01234ABCDE)")
    oldest: Optional[str] = Field(
        default=None,
        description="Start of time range as ISO 8601 timestamp (e.g. 2024-01-15T10:00:00Z). Messages after this time are returned.",
    )
    latest: Optional[str] = Field(
        default=None,
        description="End of time range as ISO 8601 timestamp. Messages before this time are returned.",
    )
    limit: int = Field(
        default=50,
        description="Maximum number of messages to return (1-200)",
    )


class GetThreadRepliesArgs(BaseModel):
    """Arguments for fetching thread replies."""
    channel_id: str = Field(description="The Slack channel ID containing the thread")
    thread_ts: str = Field(description="The timestamp of the parent message (thread_ts)")
    limit: int = Field(
        default=50,
        description="Maximum number of replies to return (1-200)",
    )


def is_slack_connected(user_id: str) -> bool:
    """Check if a user has Slack connected."""
    try:
        return get_slack_client_for_user(user_id) is not None
    except Exception:
        return False


def _iso_to_slack_ts(iso_str: Optional[str]) -> Optional[str]:
    """Convert ISO 8601 timestamp to Slack epoch timestamp."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return str(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _format_message(msg: dict) -> dict:
    """Format a Slack message for the agent's consumption."""
    ts = msg.get("ts", "")
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError):
        time_str = ts

    return {
        "ts": ts,
        "time": time_str,
        "user": msg.get("user", msg.get("username", "unknown")),
        "text": msg.get("text", ""),
        "thread_ts": msg.get("thread_ts"),
        "reply_count": msg.get("reply_count", 0),
    }


def list_slack_channels(user_id: str | None = None, **kwargs) -> str:
    """List Slack channels the bot is a member of, including name, topic, and purpose."""
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})

    client = get_slack_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})

    try:
        channels = client.list_bot_channels()
        result = []
        for ch in channels[:100]:
            result.append({
                "id": ch.get("id"),
                "name": ch.get("name"),
                "topic": (ch.get("topic") or {}).get("value", ""),
                "purpose": (ch.get("purpose") or {}).get("value", ""),
                "num_members": ch.get("num_members", 0),
                "is_member": ch.get("is_member", False),
            })

        return json.dumps({
            "status": "ok",
            "channels": result,
            "total": len(result),
        })

    except Exception as e:
        logger.info("[SlackTool] Failed to list channels: %s", type(e).__name__)
        return json.dumps({"error": f"Failed to list Slack channels: {e}"})


def get_channel_history(
    channel_id: str,
    oldest: Optional[str] = None,
    latest: Optional[str] = None,
    limit: int = 50,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Fetch messages from a Slack channel within an optional time window."""
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})

    if not channel_id:
        return json.dumps({"error": "channel_id is required."})

    client = get_slack_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})

    limit = max(1, min(limit, 200))

    try:
        params = {
            "channel": channel_id,
            "limit": limit,
        }
        oldest_ts = _iso_to_slack_ts(oldest)
        latest_ts = _iso_to_slack_ts(latest)
        if oldest_ts:
            params["oldest"] = oldest_ts
        if latest_ts:
            params["latest"] = latest_ts

        result = client._make_request("GET", "conversations.history", params)
        messages = result.get("messages", [])

        formatted = [_format_message(m) for m in messages]

        return json.dumps({
            "status": "ok",
            "channel_id": channel_id,
            "messages": formatted,
            "count": len(formatted),
        })

    except ValueError as e:
        if "not_in_channel" in str(e):
            return json.dumps({"error": f"Bot is not a member of channel {channel_id}. The bot must be invited to the channel first."})
        return json.dumps({"error": f"Slack API error: {e}"})
    except Exception as e:
        logger.info("[SlackTool] Failed to get channel history for %s", channel_id)
        return json.dumps({"error": f"Failed to fetch channel history: {e}"})


def get_thread_replies(
    channel_id: str,
    thread_ts: str,
    limit: int = 50,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Fetch replies in a Slack thread."""
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})

    if not channel_id or not thread_ts:
        return json.dumps({"error": "channel_id and thread_ts are required."})

    client = get_slack_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})

    limit = max(1, min(limit, 200))

    try:
        params = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": limit,
        }

        result = client._make_request("GET", "conversations.replies", params)
        messages = result.get("messages", [])

        formatted = [_format_message(m) for m in messages]

        return json.dumps({
            "status": "ok",
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "messages": formatted,
            "count": len(formatted),
        })

    except ValueError as e:
        return json.dumps({"error": f"Slack API error: {e}"})
    except Exception as e:
        logger.info("[SlackTool] Failed to get thread replies for %s/%s", channel_id, thread_ts)
        return json.dumps({"error": f"Failed to fetch thread replies: {e}"})
