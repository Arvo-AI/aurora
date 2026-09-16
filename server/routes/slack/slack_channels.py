"""
Slack channel management endpoints.

Mirrors :mod:`routes.github.github_repo_selection`: Aurora keeps a list of the
Slack channels it can see in the ``slack_channels`` table, each with an
LLM-generated (and user/agent-editable) description in ``metadata_summary``.
The agent queries these descriptions to decide which channel(s) are relevant
for a given incident/notification.

Endpoints (all behind ``connectors`` RBAC):

    GET    /slack/channels                       -> available (live) + connected (stored)
    POST   /slack/channels                       -> sync the set Aurora is aware of
    DELETE /slack/channels                       -> forget all channels
    PUT    /slack/channels/<channel_id>/metadata -> human-edit a channel description
    PUT    /slack/channels/<channel_id>/notify   -> toggle notify_enabled for a channel
    POST   /slack/channels/metadata/generate     -> (re)generate a channel description
"""
import json
import logging
import re

from flask import Blueprint, jsonify, request

from connectors.slack_connector.client import get_slack_client_for_user
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.db.org_scope import resolve_org, org_read_predicate
from utils.log_sanitizer import sanitize

slack_channels_bp = Blueprint("slack_channels", __name__)
logger = logging.getLogger(__name__)

# Cap on how many channels get an auto-generated description on connect, to
# bound LLM cost/volume in large workspaces. ALL channels are still registered
# (so Aurora is aware of them); only the most recent this-many are described.
MAX_AUTO_CHANNELS = 50


# Word-boundary patterns for incident-platform detection. Using anchored regex
# (rather than a bare substring `in` check) both avoids matching a platform
# name embedded in an unrelated token and clears CodeQL's "incomplete URL
# substring sanitization" rule, which flags substring checks on URL-like text.
_PLATFORM_PATTERNS = (
    ("incident.io", re.compile(r"\bincident\.io\b|\bincidentio\b")),
    ("pagerduty", re.compile(r"\bpagerduty\b|\bpd-incident\b")),
    ("opsgenie", re.compile(r"\bopsgenie\b")),
)


def _classify_channel(channel: dict) -> tuple[str, str | None]:
    """Best-effort (channel_type, detected_platform) from a Slack channel dict.

    Generic heuristic (per product requirement: support any platform that
    creates channels, e.g. incident.io/PagerDuty/Opsgenie). The LLM description
    task refines this later; this is only the fast, offline first guess so the
    UI/agent have something immediately.
    """
    name = (channel.get("name") or "").lower()
    topic = ((channel.get("topic") or {}).get("value") or "").lower()
    purpose = ((channel.get("purpose") or {}).get("value") or "").lower()
    haystack = f"{name} {topic} {purpose}"

    # Detect the incident-management platform that spawned the channel, if any.
    platform = None
    for platform_name, pattern in _PLATFORM_PATTERNS:
        if pattern.search(haystack):
            platform = platform_name
            break

    # Incident channels: platform-created OR named like one.
    if platform or name.startswith(("incident", "inc-", "inc_")) or "incident" in name:
        return "incident", platform
    # Alerting/on-call channels are still team-facing routing targets.
    if any(k in name for k in ("alert", "oncall", "on-call", "sev")):
        return "team", platform
    return "general", platform


def _upsert_channel(cur, user_id: str, org_id: str | None, ch: dict,
                    existing: dict, notify_default: bool = False,
                    initial_status: str = "pending") -> tuple[str | None, bool]:
    """Upsert one channel row. Returns (channel_id, was_newly_added).

    ``existing`` maps channel_id -> owner_user_id and is mutated in place so a
    channel already connected by another org member keeps its original owner on
    the conflict key (mirrors github save_repo_selections).

    ``initial_status`` is the metadata_status for a NEW row: 'pending' when a
    description will be generated, 'skipped' when the channel is registered for
    awareness but intentionally not described (bulk auto-register beyond the
    recency cap). Existing rows never have their status reset (ON CONFLICT does
    not touch metadata_status), so a prior 'ready' description is preserved.
    """
    channel_id = ch.get("channel_id") or ch.get("id")
    if not channel_id:
        return None, False
    owner_id = existing.get(channel_id, user_id)
    channel_type, platform = _classify_channel(ch)
    cur.execute(
        """INSERT INTO slack_channels
               (user_id, org_id, provider, team_id, channel_id, channel_name,
                is_private, is_member, channel_type, detected_platform,
                notify_enabled, channel_data, metadata_status)
           VALUES (%s, %s, 'slack', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, provider, channel_id) DO UPDATE SET
               channel_name = EXCLUDED.channel_name,
               is_private = EXCLUDED.is_private,
               is_member = EXCLUDED.is_member,
               channel_type = EXCLUDED.channel_type,
               detected_platform = EXCLUDED.detected_platform,
               channel_data = EXCLUDED.channel_data,
               updated_at = NOW()""",
        (
            owner_id, org_id, ch.get("team_id"), channel_id,
            ch.get("channel_name") or ch.get("name"),
            ch.get("is_private", False), ch.get("is_member", False),
            channel_type, platform,
            bool(ch.get("notify_enabled", notify_default)),
            json.dumps(ch),
            initial_status,
        ),
    )
    is_new = channel_id not in existing
    if is_new:
        existing[channel_id] = user_id
    return channel_id, is_new


def _rank_channels(channels: list[dict]) -> list[dict]:
    """Order channels for auto-registration: member channels first (Aurora can
    read their history), then by recency (Slack ``created`` epoch, newest
    first) as a proxy for "most recent"."""
    return sorted(
        channels,
        key=lambda c: (bool(c.get("is_member")), c.get("created") or 0),
        reverse=True,
    )


def auto_register_channels(user_id: str, team_id: str | None = None,
                           describe_limit: int = MAX_AUTO_CHANNELS) -> int:
    """Auto-register the workspace's channels on connect.

    Registers ALL visible channels so Aurora is aware of every channel (the
    agent can still route by name even without a description). To bound LLM
    cost/volume, descriptions are only generated for the ``describe_limit`` most
    recent channels (ranked member-first, then by recency) — the rest stay
    ``metadata_status='pending'`` with no description until one is explicitly
    requested. Best-effort; returns the number of descriptions enqueued.
    Idempotent — existing rows are updated, not duplicated.
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return 0
        channels = client.list_all_channels()
    except Exception:
        logger.warning("[slack_channels] auto-register: failed to list channels", exc_info=True)
        return 0

    if not channels:
        return 0

    # Rank once; the top slice is what we describe, but we register everything.
    ranked = _rank_channels(channels)
    describe_ids = {c.get("id") for c in ranked[:describe_limit]}

    org_id = resolve_org(user_id)
    newly_added_to_describe: list[str] = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:auto]")
                cur.execute(
                    "SELECT channel_id, user_id FROM slack_channels WHERE provider = 'slack'"
                )
                existing = {r[0]: r[1] for r in cur.fetchall()}
                for ch in ranked:
                    ch = {**ch, "channel_id": ch.get("id"), "channel_name": ch.get("name"),
                          "team_id": team_id}
                    # Describe only the top-N recent channels; register the rest
                    # for awareness with 'skipped' so the UI doesn't show a
                    # perpetual "generating" spinner for them.
                    will_describe = ch["channel_id"] in describe_ids
                    _cid, is_new = _upsert_channel(
                        cur, user_id, org_id, ch, existing,
                        initial_status="pending" if will_describe else "skipped",
                    )
                    if is_new and _cid and will_describe:
                        newly_added_to_describe.append(_cid)
                conn.commit()
    except Exception:
        logger.warning("[slack_channels] auto-register: DB upsert failed", exc_info=True)
        return 0

    for channel_id in newly_added_to_describe:
        _enqueue_metadata(user_id, channel_id)
    logger.info(
        "[slack_channels] auto-registered %d channel(s), describing %d",
        len(ranked), len(newly_added_to_describe),
    )
    return len(newly_added_to_describe)


@slack_channels_bp.route("/channels", methods=["GET"])
@require_permission("connectors", "read")
def get_slack_channels(user_id):
    """Return connected (stored) channels plus the live available list."""
    try:
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        # Stored channels for this org.
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:list]")
                cur.execute(
                    f"""SELECT DISTINCT ON (channel_id)
                              channel_id, channel_name, is_private, is_member,
                              channel_type, detected_platform, notify_enabled,
                              metadata_summary, metadata_status
                         FROM slack_channels
                        WHERE provider = 'slack' AND {predicate}
                        ORDER BY channel_id, updated_at DESC""",
                    pred_params,
                )
                rows = cur.fetchall()

        connected = [
            {
                "channel_id": r[0],
                "channel_name": r[1],
                "is_private": r[2],
                "is_member": r[3],
                "channel_type": r[4],
                "detected_platform": r[5],
                "notify_enabled": r[6],
                "metadata_summary": r[7],
                "metadata_status": r[8],
            }
            for r in rows
        ]

        # Live available list (best-effort — never fail the whole response).
        available = []
        try:
            client = get_slack_client_for_user(user_id)
            if client:
                for ch in client.list_all_channels():
                    available.append({
                        "channel_id": ch.get("id"),
                        "channel_name": ch.get("name"),
                        "is_private": ch.get("is_private", False),
                        "is_member": ch.get("is_member", False),
                        "topic": (ch.get("topic") or {}).get("value", ""),
                        "purpose": (ch.get("purpose") or {}).get("value", ""),
                        "num_members": ch.get("num_members"),
                    })
        except Exception:
            logger.warning("Failed to list available Slack channels", exc_info=True)

        return jsonify({"connected": connected, "available": available})
    except Exception:
        logger.exception("Error getting Slack channels")
        return jsonify({"error": "Failed to get Slack channels"}), 500


@slack_channels_bp.route("/channels", methods=["POST"])
@require_permission("connectors", "write")
def save_slack_channels(user_id):
    """Sync the set of channels Aurora is aware of. Upserts new, removes dropped."""
    try:
        data = request.get_json(silent=True) or {}
        channels = data.get("channels")
        if not isinstance(channels, list) or not all(isinstance(c, dict) for c in channels):
            return jsonify({"error": "channels must be an array of objects"}), 400

        org_id = resolve_org(user_id)

        newly_added = []
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:save]")
                cur.execute(
                    "SELECT channel_id, user_id FROM slack_channels WHERE provider = 'slack'"
                )
                # {channel_id: owner_user_id} — need the owner to delete the right row.
                existing = {r[0]: r[1] for r in cur.fetchall()}

                incoming = set()
                for ch in channels:
                    channel_id, is_new = _upsert_channel(cur, user_id, org_id, ch, existing)
                    if not channel_id:
                        continue
                    incoming.add(channel_id)
                    if is_new:
                        newly_added.append(channel_id)

                removed = set(existing.keys()) - incoming
                if removed:
                    cur.execute(
                        "DELETE FROM slack_channels WHERE provider = 'slack' AND channel_id = ANY(%s)",
                        (list(removed),),
                    )
                conn.commit()

        # Kick off description generation for genuinely new channels.
        for channel_id in newly_added:
            _enqueue_metadata(user_id, channel_id)

        return jsonify({
            "message": f"Saved {len(incoming)} channels, removed {len(removed)}",
            "added": newly_added,
            "removed": list(removed),
        })
    except Exception:
        logger.exception("Error saving Slack channels")
        return jsonify({"error": "Failed to save Slack channels"}), 500


@slack_channels_bp.route("/channels", methods=["DELETE"])
@require_permission("connectors", "write")
def clear_slack_channels(user_id):
    """Forget all channels for the org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:clear]")
                cur.execute("DELETE FROM slack_channels WHERE provider = 'slack'")
                conn.commit()
        return jsonify({"message": "All Slack channels cleared"})
    except Exception:
        logger.exception("Error clearing Slack channels")
        return jsonify({"error": "Failed to clear Slack channels"}), 500


@slack_channels_bp.route("/channels/<channel_id>/metadata", methods=["PUT"])
@require_permission("connectors", "write")
def update_channel_metadata(user_id, channel_id):
    """Human edit of a channel description."""
    try:
        data = request.get_json(silent=True) or {}
        summary = data.get("metadata_summary")
        if summary is None:
            return jsonify({"error": "metadata_summary is required"}), 400
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:metadata]")
                cur.execute(
                    """UPDATE slack_channels
                       SET metadata_summary = %s, metadata_status = 'ready', updated_at = NOW()
                       WHERE provider = 'slack' AND channel_id = %s""",
                    (summary, channel_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"error": "Channel not found"}), 404
                conn.commit()
        return jsonify({"message": "Metadata updated"})
    except Exception:
        logger.exception("Error updating channel metadata")
        return jsonify({"error": "Failed to update metadata"}), 500


@slack_channels_bp.route("/channels/<channel_id>/notify", methods=["PUT"])
@require_permission("connectors", "write")
def update_channel_notify(user_id, channel_id):
    """Toggle whether Aurora may proactively notify a channel."""
    try:
        data = request.get_json(silent=True) or {}
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:notify]")
                cur.execute(
                    """UPDATE slack_channels
                       SET notify_enabled = %s, updated_at = NOW()
                       WHERE provider = 'slack' AND channel_id = %s""",
                    (enabled, channel_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"error": "Channel not found"}), 404
                conn.commit()
        return jsonify({"channel_id": channel_id, "notify_enabled": enabled})
    except Exception:
        logger.exception("Error updating channel notify flag")
        return jsonify({"error": "Failed to update notify flag"}), 500


@slack_channels_bp.route("/channels/metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_channel_metadata(user_id):
    """(Re)generate the LLM description for a specific channel."""
    try:
        data = request.get_json(silent=True) or {}
        channel_id = data.get("channel_id")
        if not channel_id:
            return jsonify({"error": "channel_id is required"}), 400
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:regen]")
                cur.execute(
                    """UPDATE slack_channels SET metadata_status = 'generating', updated_at = NOW()
                       WHERE provider = 'slack' AND channel_id = %s""",
                    (channel_id,),
                )
                conn.commit()
        _enqueue_metadata(user_id, channel_id)
        return jsonify({"message": "Metadata generation started"})
    except Exception:
        logger.exception("Error triggering channel metadata")
        return jsonify({"error": "Failed to trigger metadata generation"}), 500


def _enqueue_metadata(user_id: str, channel_id: str):
    """Enqueue the description task; mark 'error' on the row if enqueue fails."""
    try:
        from routes.slack.slack_channel_metadata import generate_channel_metadata
        generate_channel_metadata.delay(user_id, channel_id)
    except Exception as e:
        logger.warning("Failed to enqueue channel metadata for %s: %s", sanitize(channel_id), e)
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:enqueue_err]")
                    cur.execute(
                        """UPDATE slack_channels SET metadata_status = 'error', updated_at = NOW()
                           WHERE provider = 'slack' AND channel_id = %s""",
                        (channel_id,),
                    )
                    conn.commit()
        except Exception:
            logger.debug("Could not mark channel metadata error", exc_info=True)
