"""Microsoft Teams Bot Framework message endpoint."""

import logging
import re
import time

from flask import Blueprint, jsonify, request

from routes.teams import teams_installs
from routes.teams.teams_lifecycle import handle_conversation_update
from routes.teams.teams_events_helpers import (
    claim_activity_once,
    extract_team_and_channel_ids,
    get_user_id_from_teams_tenant,
    is_bot_mentioned,
    is_trusted_service_url,
    send_message_to_aurora,
    verify_teams_request_claims,
)

teams_events_bp = Blueprint("teams_events", __name__)
logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<at>.*?</at>", re.IGNORECASE)


@teams_events_bp.route("/messages", methods=["POST"])
def teams_messages():
    claims = verify_teams_request_claims()
    if claims is None:
        return jsonify({"error": "Unauthorized"}), 403

    activity = request.get_json(silent=True) or {}
    activity_type = activity.get("type")
    if activity_type not in ("message", "conversationUpdate"):
        return jsonify({}), 200

    # Replies go out with the bot's token, so the target host must be Microsoft's
    service_url = activity.get("serviceUrl")
    if not is_trusted_service_url(service_url, claims):
        logger.warning("Teams activity rejected: untrusted serviceUrl")
        return jsonify({"error": "Unauthorized"}), 403

    # Bot Framework retries slow POSTs — a repeat of an activity we've seen is dropped
    if not claim_activity_once(activity.get("id")):
        return jsonify({}), 200

    conversation = activity.get("conversation") or {}

    # Install/channel changes keep our team state in sync; no reply is sent
    if activity_type == "conversationUpdate":
        _apply_conversation_update(activity, conversation, service_url)
        return jsonify({}), 200

    is_dm = conversation.get("conversationType") == "personal"
    # Channels and group chats only respond when @mentioned (Slack parity); DMs always count.
    if not is_dm and not is_bot_mentioned(activity):
        return jsonify({}), 200

    text = (activity.get("text") or "").strip()
    text = _MENTION_RE.sub("", text).strip()
    if not text:
        return jsonify({}), 200

    tenant_id = conversation.get("tenantId") or (activity.get("channelData") or {}).get("tenant", {}).get("id")
    user_id = get_user_id_from_teams_tenant(tenant_id)
    if not user_id:
        logger.warning("Teams message from unknown tenant")
        return jsonify({}), 200

    team_id, channel_id = extract_team_and_channel_ids(activity)
    reply_to_id = activity.get("id")

    # A DM has no team; a channel message without one can't be tied to a channel
    if not channel_id or (not team_id and not is_dm):
        logger.warning("Teams message missing team_id or channel_id")
        return jsonify({}), 200

    # A message from a team proves the bot is installed there
    try:
        # Only the Graph GUID is a usable key; the thread-style fallback isn't
        graph_team_id = ((activity.get("channelData") or {}).get("team") or {}).get("aadGroupId")
        if graph_team_id:
            teams_installs.mark_team_installed(user_id, graph_team_id)
        teams_installs.remember_service_url(user_id, service_url)
    except Exception:
        logger.debug("Failed to record Teams install state", exc_info=True)

    try:
        from utils.auth.stateless_auth import get_org_id_for_user, store_org_preference

        org_id = get_org_id_for_user(user_id)
        if org_id:
            store_org_preference(org_id, "teams_last_bot_message_at", str(int(time.time())))
    except Exception:
        logger.debug("Failed to record Teams bot activity timestamp", exc_info=True)

    try:
        send_message_to_aurora(
            user_id,
            text,
            team_id=team_id,
            channel_id=channel_id,
            reply_to_id=reply_to_id,
            service_url=service_url,
            conversation_id=conversation.get("id"),
        )
    except Exception:
        logger.exception("Failed to dispatch Teams message to Aurora")
    return jsonify({}), 200


def _apply_conversation_update(activity: dict, conversation: dict, service_url: str) -> None:
    tenant_id = conversation.get("tenantId") or (activity.get("channelData") or {}).get("tenant", {}).get("id")
    user_id = get_user_id_from_teams_tenant(tenant_id)
    # Not a tenant we have connected
    if not user_id:
        return
    try:
        teams_installs.remember_service_url(user_id, service_url)
        handle_conversation_update(user_id, activity)
    except Exception:
        logger.exception("Failed to apply Teams conversationUpdate")
