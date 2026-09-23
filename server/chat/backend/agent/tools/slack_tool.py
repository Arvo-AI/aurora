"""
Slack Tools for Agent

Agent-callable tools for reading Slack channel messages/threads and for posting
a message (new or threaded reply) back into a channel.

The read tools gather human context from team conversations during incidents
(e.g. the "Generate Postmortem" action). The single write tool, post_slack_message,
lets the team-routing agent act like a teammate: after reading a channel it can
either start a new message or reply in an existing thread (thread_ts) so a
recurring incident threads under the prior conversation instead of adding noise.

NOTE: A shared run_slack_tool() callback pattern was considered but rejected —
with a handful of tools, the indirection decreases readability for little benefit.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from connectors.slack_connector.client import get_slack_client_for_user, SlackAPIError

logger = logging.getLogger(__name__)

_ERR_NO_USER = "No user context available."
_ERR_NOT_CONNECTED = "Slack not connected for this user."
# Slack hard-caps a single message's text; keep well under it so a post never
# fails wholesale on an over-long body (the agent should be terse anyway).
_MAX_POST_CHARS = 3000


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


class PostSlackMessageArgs(BaseModel):
    """Arguments for posting a message to a Slack channel."""
    channel_id: str = Field(description="The Slack channel ID to post to (e.g. C01234ABCDE)")
    text: str = Field(description="The message text (Slack mrkdwn). Keep it short and human.")
    thread_ts: Optional[str] = Field(
        default=None,
        description=(
            "Timestamp (ts) of an existing message to reply UNDER as a threaded "
            "reply. Set this to keep a follow-up on a recurring incident in the "
            "same thread instead of posting a new top-level message. Omit to "
            "start a new message."
        ),
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

    def _fetch():
        return client._make_request("GET", "conversations.history", params)

    try:
        # Public channels can be read without an invite: on not_in_channel we
        # join (conversations.join works for PUBLIC channels only) and retry, so
        # Aurora can read any public channel it can see — matching how a human
        # teammate would just open it. Private channels still require an invite.
        result = _read_with_public_join_retry(client, channel_id, _fetch)
        messages = result.get("messages", [])

        formatted = [_format_message(m) for m in messages]

        return json.dumps({
            "status": "ok",
            "channel_id": channel_id,
            "messages": formatted,
            "count": len(formatted),
        })

    except SlackAPIError as e:
        # Private channel Aurora hasn't been invited to (join couldn't recover it).
        if getattr(e, "error", None) == "not_in_channel":
            logger.info("[SlackTool] channel history read blocked (private, not invited) for %s", channel_id)
            return json.dumps({"error": f"Channel {channel_id} is private and the bot hasn't been invited. Invite the bot to read it."})
        logger.info("[SlackTool] Slack API error reading channel history for %s: %s", channel_id, getattr(e, "error", e))
        return json.dumps({"error": f"Slack API error: {e}"})
    except ValueError as e:
        # Transport failure (SlackAPIError's non-membership sibling).
        logger.info("[SlackTool] transport error reading channel history for %s: %s", channel_id, e)
        return json.dumps({"error": f"Slack API error: {e}"})
    except Exception as e:
        logger.info("[SlackTool] Failed to get channel history for %s", channel_id)
        return json.dumps({"error": f"Failed to fetch channel history: {e}"})


def _read_with_public_join_retry(client, channel_id: str, fetch):
    """Run ``fetch`` (a read call); on not_in_channel, join the PUBLIC channel
    once and retry.

    Slack's conversations.history/replies require membership. For public
    channels a bot can self-join via conversations.join (no human invite needed),
    so Aurora can read any public channel it can see. join_channel returns None
    for private channels (join isn't supported), so the original not_in_channel
    error propagates and the caller reports the "invite required" message.
    """
    try:
        return fetch()
    except SlackAPIError as e:
        # Only not_in_channel is recoverable by joining; match the error code
        # structurally (not substring) so transport errors / other API errors
        # don't accidentally trigger a join+retry. Re-raise everything else.
        if getattr(e, "error", None) != "not_in_channel":
            raise
        joined = client.join_channel(channel_id)  # None ⇒ private / can't join
        if not joined:
            raise
        logger.info("[SlackTool] Joined public channel %s to read it", channel_id)
        return fetch()


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

    params = {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": limit,
    }

    def _fetch():
        return client._make_request("GET", "conversations.replies", params)

    try:
        # Same public-channel self-join as get_channel_history — read any public
        # thread even if Aurora hasn't been invited.
        result = _read_with_public_join_retry(client, channel_id, _fetch)
        messages = result.get("messages", [])

        formatted = [_format_message(m) for m in messages]

        return json.dumps({
            "status": "ok",
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "messages": formatted,
            "count": len(formatted),
        })

    except SlackAPIError as e:
        # Private channel Aurora hasn't been invited to (join couldn't recover it).
        if getattr(e, "error", None) == "not_in_channel":
            logger.info("[SlackTool] thread read blocked (private, not invited) for %s/%s", channel_id, thread_ts)
            return json.dumps({"error": f"Channel {channel_id} is private and the bot hasn't been invited. Invite the bot to read it."})
        logger.info("[SlackTool] Slack API error reading thread %s/%s: %s", channel_id, thread_ts, getattr(e, "error", e))
        return json.dumps({"error": f"Slack API error: {e}"})
    except ValueError as e:
        # Transport failure (SlackAPIError's non-membership sibling).
        logger.info("[SlackTool] transport error reading thread %s/%s: %s", channel_id, thread_ts, e)
        return json.dumps({"error": f"Slack API error: {e}"})
    except Exception as e:
        logger.info("[SlackTool] Failed to get thread replies for %s/%s", channel_id, thread_ts)
        return json.dumps({"error": f"Failed to fetch thread replies: {e}"})


def post_slack_message(
    channel_id: str,
    text: str,
    thread_ts: Optional[str] = None,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Post a message to a Slack channel, optionally as a threaded reply.

    The one write tool on the Slack surface. Never raises — a failure here must
    not abort the agent loop; it returns a JSON error the agent can react to.
    Auto-joins the channel once on not_in_channel and retries, mirroring the
    notification service, so a channel Aurora can see but hasn't joined still works.
    """
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})
    if not channel_id:
        return json.dumps({"error": "channel_id is required."})

    text = (text or "").strip()
    if not text:
        return json.dumps({"error": "text is required and must be non-empty."})
    # Trim rather than fail: a slightly long teammate note should still land.
    if len(text) > _MAX_POST_CHARS:
        text = text[: _MAX_POST_CHARS - 3] + "..."

    client = get_slack_client_for_user(user_id)
    if not client:
        return json.dumps({"error": _ERR_NOT_CONNECTED})

    def _send() -> dict:
        return client.send_message(channel=channel_id, text=text, thread_ts=thread_ts or None)

    try:
        response = _send()
    except SlackAPIError as e:
        # not_in_channel → join once and retry, else surface the error.
        if getattr(e, "error", None) == "not_in_channel":
            try:
                client.join_channel(channel_id)
                response = _send()
            except Exception as retry_err:
                logger.info("[SlackTool] Post retry after join failed for %s", channel_id)
                return json.dumps({"error": f"Could not post to channel after join: {retry_err}"})
        else:
            return json.dumps({"error": f"Slack API error: {e}"})
    except Exception as e:
        logger.info("[SlackTool] Failed to post message to %s", channel_id)
        return json.dumps({"error": f"Failed to post message: {e}"})

    message_ts = (response or {}).get("ts")
    return json.dumps({
        "status": "posted",
        "channel_id": channel_id,
        "ts": message_ts,
        # Echo back whether this landed in a thread so the agent knows it did
        # not add a new top-level message.
        "threaded": bool(thread_ts),
    })


class GetConnectedSlackChannelsArgs(BaseModel):
    """No required args — reads the org's known channels from context."""
    pass


def get_connected_slack_channels(user_id: str | None = None, **kwargs) -> str:
    """Return the Slack channels Aurora is aware of, each with its description.

    This is the routing-decision source: use the descriptions to choose which
    channel(s) are relevant for a given incident/notification. Distinct from
    list_slack_channels (a live, description-less listing of bot memberships).
    """
    if not user_id:
        return json.dumps({"error": _ERR_NO_USER})

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.org_scope import resolve_org, org_read_predicate

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[SlackTool:connected]")
                cur.execute(
                    f"""SELECT DISTINCT ON (channel_id)
                              channel_id, channel_name, channel_type,
                              detected_platform, notify_enabled,
                              metadata_summary, metadata_status, is_member
                         FROM slack_channels
                        WHERE provider = 'slack' AND NOT is_dismissed AND {predicate}
                        ORDER BY channel_id, updated_at DESC""",
                    pred_params,
                )
                rows = cur.fetchall()

        channels = [
            {
                "channel_id": r[0],
                "channel_name": r[1],
                "channel_type": r[2],
                "detected_platform": r[3],
                "notify_enabled": r[4],
                "description": r[5] or (
                    "(no description — call generate if needed)" if r[6] == "skipped"
                    else "(description generating...)" if r[6] != "ready"
                    else "(no description)"
                ),
                "is_member": r[7],
            }
            for r in rows
        ]

        if not channels:
            return json.dumps({
                "channels": [],
                "message": (
                    "No Slack channels registered yet. Ask the user to invite "
                    "Aurora to channels in Slack and refresh, or use "
                    "list_slack_channels for a live listing."
                ),
            })
        return json.dumps({"channels": channels})
    except Exception as e:
        logger.exception("Error fetching connected Slack channels")
        return json.dumps({"error": f"Failed to fetch connected Slack channels: {e}"})
