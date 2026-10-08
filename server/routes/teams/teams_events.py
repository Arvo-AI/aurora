"""Microsoft Teams Bot Framework message endpoint."""

import logging
import re

from flask import Blueprint, jsonify, request

from routes.teams.teams_events_helpers import (
    get_user_id_from_teams_tenant,
    send_message_to_aurora,
    verify_teams_request,
)

teams_events_bp = Blueprint("teams_events", __name__)
logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"<at>.*?</at>", re.IGNORECASE)


@teams_events_bp.route("/messages", methods=["POST"])
def teams_messages():
    if not verify_teams_request():
        return jsonify({"error": "Unauthorized"}), 403

    activity = request.get_json(silent=True) or {}
    if activity.get("type") != "message":
        return jsonify({}), 200

    text = (activity.get("text") or "").strip()
    text = _MENTION_RE.sub("", text).strip()
    if not text:
        return jsonify({}), 200

    conversation = activity.get("conversation") or {}
    tenant_id = conversation.get("tenantId") or (activity.get("channelData") or {}).get("tenant", {}).get("id")
    user_id = get_user_id_from_teams_tenant(tenant_id)
    if not user_id:
        logger.warning("Teams message from unknown tenant")
        return jsonify({}), 200

    channel_data = activity.get("channelData") or {}
    team_id = channel_data.get("team", {}).get("id") or channel_data.get("teamsTeamId")
    channel_id = conversation.get("id") or activity.get("channelId")
    reply_to_id = activity.get("id")

    if not team_id or not channel_id:
        logger.warning("Teams message missing team_id or channel_id")
        return jsonify({}), 200

    try:
        send_message_to_aurora(
            user_id, text, team_id=team_id, channel_id=channel_id, reply_to_id=reply_to_id,
        )
    except Exception:
        logger.exception("Failed to dispatch Teams message to Aurora")
    return jsonify({}), 200
