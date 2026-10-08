"""Microsoft Teams channel registration (uses shared ``slack_channels`` table)."""

import json
import logging

from flask import Blueprint, jsonify

from connectors.teams_connector.client import get_teams_client_for_user
from services.channels.classify import classify_channel
from services.channels import registry
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.db.org_scope import resolve_org
from utils.log_sanitizer import sanitize

teams_channels_bp = Blueprint("teams_channels", __name__)
logger = logging.getLogger(__name__)

_PROVIDER = "teams"
_MAX_AUTO = 50


def _classify_channel(name: str, description: str) -> tuple[str, str | None]:
    text = f"{name} {description}".lower()
    return classify_channel(name.lower(), text)


def auto_register_channels(user_id: str) -> int:
    """Register member channels from every joined team (capped). Returns count registered."""
    client = get_teams_client_for_user(user_id)
    if not client:
        return 0
    org_id = resolve_org(user_id)
    registered = 0
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:register]")
            existing: dict[str, str] = {}
            cur.execute(
                """SELECT channel_id, user_id FROM slack_channels
                   WHERE provider = %s AND org_id = %s""",
                (_PROVIDER, org_id),
            )
            for row in cur.fetchall():
                existing[row[0]] = row[1]

            for team in client.list_joined_teams():
                if registered >= _MAX_AUTO:
                    break
                team_id = team.get("id")
                team_name = team.get("displayName") or "team"
                for ch in client.list_team_channels(team_id):
                    if registered >= _MAX_AUTO:
                        break
                    channel_id = ch.get("id")
                    if not channel_id:
                        continue
                    name = ch.get("displayName") or "channel"
                    desc = ch.get("description") or ""
                    channel_type, platform = _classify_channel(name, desc)
                    payload = {
                        "channel_id": channel_id,
                        "team_id": team_id,
                        "channel_name": name,
                        "name": name,
                        "is_private": ch.get("membershipType") == "private",
                        "is_member": True,
                        "team_name": team_name,
                    }
                    summary = (desc or "").strip() or f"{name} in {team_name}"
                    _, is_new = registry.upsert_channel(
                        cur, user_id, org_id, _PROVIDER, payload, existing,
                        channel_type=channel_type,
                        detected_platform=platform,
                        initial_status="ready",
                    )
                    cur.execute(
                        """UPDATE slack_channels
                           SET metadata_summary = %s, metadata_status = 'ready'
                           WHERE user_id = %s AND provider = %s AND channel_id = %s
                             AND (metadata_summary IS NULL OR metadata_summary = '')""",
                        (summary, existing.get(channel_id, user_id), _PROVIDER, channel_id),
                    )
                    if is_new:
                        registered += 1
            conn.commit()
    except Exception:
        logger.exception("[TeamsChannels] auto_register failed for user %s", sanitize(user_id))
        return registered
    logger.info("[TeamsChannels] registered %d channel(s) for user %s", registered, sanitize(user_id))
    return registered


@teams_channels_bp.route("/channels/refresh", methods=["POST"])
@require_permission("connectors", "write")
def refresh_channels(user_id):
    count = auto_register_channels(user_id)
    return jsonify({"success": True, "registered": count}), 200


@teams_channels_bp.route("/channels", methods=["GET"])
@require_permission("connectors", "read")
def list_channels(user_id):
    try:
        channels = registry.get_connected_channels(user_id, _PROVIDER)
        return jsonify({"connected": channels}), 200
    except Exception:
        logger.exception("[TeamsChannels] list failed")
        return jsonify({"error": "Failed to list Teams channels"}), 500
