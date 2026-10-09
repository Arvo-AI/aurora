"""Microsoft Teams channel management (shared ``slack_channels`` table, provider ``teams``)."""

from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify, request

from connectors.teams_connector.client import TeamsClient, get_teams_client_for_user
from routes.slack.slack_backfill_config import BACKFILL_STALE_MINUTES
from services.channels import prefs as channel_prefs
from services.channels import registry
from services.channels.classify import classify_channel
from services.channels.update_row import update_one_channel
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context, store_org_preference
from utils.cache.redis_client import get_redis_client
from utils.db.connection_pool import db_pool
from utils.db.org_scope import org_read_predicate, resolve_org
from utils.log_sanitizer import sanitize

teams_channels_bp = Blueprint("teams_channels", __name__)
logger = logging.getLogger(__name__)

_PROVIDER = "teams"
MAX_AUTO_CHANNELS = 50
MAX_ACTIVATE_DESCRIBE = 50
AVAILABLE_CHANNELS_CACHE_TTL = 300
LIST_CHANNELS_CAP = 2000


def purge_teams_connector_data(user_id: str) -> None:
    """Drop Teams channel rows and org prefs after OAuth disconnect."""
    org_id = get_org_id_for_user(user_id)
    if org_id:
        store_org_preference(org_id, channel_prefs.incidents_channel_pref_key(_PROVIDER), "")
        store_org_preference(org_id, channel_prefs.incidents_channel_name_pref_key(_PROVIDER), "")
        store_org_preference(org_id, channel_prefs.hidden_channels_pref_key(_PROVIDER), "[]")
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:purge]")
        cur.execute("DELETE FROM slack_channels WHERE provider = %s", (_PROVIDER,))
        conn.commit()


def _classify_channel(name: str, description: str) -> tuple[str, str | None]:
    text = f"{name} {description}".lower()
    return classify_channel(name.lower(), text)


def _team_id_for_user(user_id: str) -> str | None:
    try:
        from utils.auth.stateless_auth import get_credentials_from_db

        return (get_credentials_from_db(user_id, _PROVIDER) or {}).get("tenant_id")
    except Exception:
        logger.warning("[TeamsChannels] could not resolve tenant for user %s", sanitize(user_id))
        return None


def _team_id_for_channel(user_id: str, channel_id: str) -> str | None:
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:team_lookup]")
            cur.execute(
                """SELECT team_id FROM slack_channels
                   WHERE provider = %s AND channel_id = %s LIMIT 1""",
                (_PROVIDER, channel_id),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception:
        logger.debug("[TeamsChannels] team_id lookup failed for %s", sanitize(channel_id), exc_info=True)
    client = get_teams_client_for_user(user_id)
    if not client:
        return None
    for team in client.list_joined_teams():
        team_id = team.get("id")
        if not team_id:
            continue
        for ch in client.list_team_channels(team_id):
            if ch.get("id") == channel_id:
                return team_id
    return None


def _upsert_channel(cur, user_id: str, org_id: str | None, ch: dict, existing: dict, *,
                    initial_status: str = "pending") -> tuple[str | None, bool]:
    channel_type, platform = _classify_channel(
        ch.get("channel_name") or ch.get("name") or "",
        ch.get("description") or "",
    )
    return registry.upsert_channel(
        cur, user_id, org_id, _PROVIDER, ch, existing,
        initial_status=initial_status, channel_type=channel_type, detected_platform=platform,
    )


def _enqueue_metadata(user_id: str, channel_id: str, team_id: str | None = None) -> None:
    try:
        from routes.teams.teams_channel_metadata import generate_channel_metadata

        generate_channel_metadata.delay(user_id, channel_id, team_id)
    except Exception as e:
        logger.warning("Failed to enqueue Teams metadata for %s: %s", sanitize(channel_id), sanitize(e))
        try:
            with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:enqueue_err]")
                cur.execute(
                    """UPDATE slack_channels SET metadata_status = 'error', updated_at = NOW()
                       WHERE provider = %s AND channel_id = %s""",
                    (_PROVIDER, channel_id),
                )
                conn.commit()
        except Exception:
            logger.debug("Could not mark Teams channel metadata error", exc_info=True)


def _mark_pending(cur, channel_ids: list[str], stale_minutes: int | None = None) -> list[str]:
    if not channel_ids:
        return []
    if stale_minutes is not None:
        cur.execute(
            """UPDATE slack_channels
                  SET metadata_status = 'pending', updated_at = NOW()
                WHERE provider = %s AND channel_id = ANY(%s)
                  AND (metadata_status = 'skipped'
                       OR (metadata_status IN ('pending', 'generating')
                           AND updated_at < NOW() - make_interval(mins => %s)))
             RETURNING channel_id""",
            (_PROVIDER, channel_ids, stale_minutes),
        )
    else:
        cur.execute(
            """UPDATE slack_channels
                  SET metadata_status = 'pending', updated_at = NOW()
                WHERE provider = %s AND channel_id = ANY(%s)
                  AND metadata_status IN ('pending', 'skipped')
             RETURNING channel_id""",
            (_PROVIDER, channel_ids),
        )
    return list(dict.fromkeys(row[0] for row in cur.fetchall()))


def _backdate_for_backfill(cur, channel_id: str) -> None:
    cur.execute(
        """UPDATE slack_channels
              SET updated_at = NOW() - make_interval(mins => %s)
            WHERE provider = %s AND channel_id = %s
              AND metadata_status = 'pending'""",
        (BACKFILL_STALE_MINUTES + 1, _PROVIDER, channel_id),
    )


def _enumerate_member_channels(user_id: str, client: TeamsClient | None = None) -> list[dict]:
    """All channels in joined teams minus org-hidden ids."""
    if client is None:
        client = get_teams_client_for_user(user_id)
    if not client:
        return []
    hidden = channel_prefs.get_hidden_channel_ids(user_id, _PROVIDER)
    out: list[dict] = []
    for team in client.list_joined_teams():
        team_id = team.get("id")
        team_name = team.get("displayName") or "team"
        if not team_id:
            continue
        for ch in client.list_team_channels(team_id):
            if len(out) >= LIST_CHANNELS_CAP:
                return out
            channel_id = ch.get("id")
            if not channel_id or channel_id in hidden:
                continue
            name = ch.get("displayName") or "channel"
            desc = ch.get("description") or ""
            out.append({
                "channel_id": channel_id,
                "team_id": team_id,
                "channel_name": name,
                "name": name,
                "description": desc,
                "is_private": ch.get("membershipType") == "private",
                "is_member": True,
                "team_name": team_name,
            })
    return out


def auto_register_channels(user_id: str, team_id: str | None = None, *,
                           describe_limit: int = MAX_AUTO_CHANNELS) -> int:
    client = get_teams_client_for_user(user_id)
    if not client:
        logger.warning(
            "[TeamsChannels] skip sync — no Graph client for user %s",
            sanitize(user_id),
        )
        return 0
    member_channels = _enumerate_member_channels(user_id, client)
    member_ids = {c["channel_id"] for c in member_channels}

    org_id = resolve_org(user_id)
    newly_added: list[str] = []
    team_by_channel = {c["channel_id"]: c["team_id"] for c in member_channels}
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:auto]")
            cur.execute(
                """SELECT channel_id, user_id, metadata_status,
                          (updated_at < NOW() - INTERVAL '10 minutes') AS is_stale
                     FROM slack_channels WHERE provider = %s""",
                (_PROVIDER,),
            )
            rows = cur.fetchall()
            existing = {r[0]: r[1] for r in rows}
            status_by_id = {r[0]: r[2] for r in rows}
            is_stale_by_id = {r[0]: bool(r[3]) for r in rows}

            for ch in member_channels:
                cid = ch["channel_id"]
                status = status_by_id.get(cid)
                needs_describe = (
                    status in (None, "skipped")
                    or (status == "pending" and is_stale_by_id.get(cid, False))
                )
                _cid, is_new = _upsert_channel(cur, user_id, org_id, ch, existing, initial_status="pending")
                if _cid and (is_new or needs_describe) and len(newly_added) < describe_limit:
                    newly_added.append(_cid)

            newly_added = _mark_pending(cur, newly_added)

            pruned_card = False
            if existing:
                stale_ids = [cid for cid in existing if cid not in member_ids]
                if stale_ids:
                    card_id = _get_card_channel_id(user_id)
                    pruned_card = bool(card_id and card_id in stale_ids)
                    cur.execute(
                        """DELETE FROM slack_channels
                           WHERE provider = %s AND channel_id = ANY(%s)""",
                        (_PROVIDER, stale_ids),
                    )
            conn.commit()
    except Exception:
        logger.warning("[TeamsChannels] auto_register DB failed", exc_info=True)
        return 0

    if pruned_card:
        try:
            _clear_card_channel(user_id)
        except Exception:
            logger.warning("[TeamsChannels] failed to clear pruned card channel", exc_info=True)

    for channel_id in newly_added:
        _enqueue_metadata(user_id, channel_id, team_by_channel.get(channel_id))
    return len(newly_added)


def register_single_channel(
    user_id: str,
    channel_id: str,
    team_id: str | None = None,
    *,
    describe: bool = True,
) -> bool:
    if not channel_id:
        return False
    tid = team_id or _team_id_for_channel(user_id, channel_id)
    if not tid:
        return False
    client = get_teams_client_for_user(user_id)
    if not client:
        return False
    info = client.get_channel(tid, channel_id) or {}
    name = info.get("displayName") or "channel"
    ch = {
        "channel_id": channel_id,
        "team_id": tid,
        "channel_name": name,
        "name": name,
        "description": info.get("description") or "",
        "is_private": info.get("membershipType") == "private",
        "is_member": True,
    }
    org_id = resolve_org(user_id)
    is_new = False
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:register_one]")
            cur.execute(
                """SELECT user_id FROM slack_channels
                   WHERE provider = %s AND channel_id = %s""",
                (_PROVIDER, channel_id),
            )
            row = cur.fetchone()
            existing = {channel_id: row[0]} if row else {}
            _cid, is_new = _upsert_channel(cur, user_id, org_id, ch, existing, initial_status="pending")
            if is_new and _cid and not describe:
                _backdate_for_backfill(cur, channel_id)
            conn.commit()
    except Exception:
        logger.warning("[TeamsChannels] single register failed", exc_info=True)
        return False

    channel_prefs.remove_hidden_channel(user_id, _PROVIDER, channel_id)
    _invalidate_available_cache(user_id)
    if is_new and describe:
        _enqueue_metadata(user_id, channel_id, tid)
    return is_new


def _activate_channels(user_id: str, channel_ids: list[str]) -> int:
    activated = 0
    described = 0
    for channel_id in channel_ids:
        tid = _team_id_for_channel(user_id, channel_id)
        if not tid:
            continue
        describe = described < MAX_ACTIVATE_DESCRIBE
        if register_single_channel(user_id, channel_id, team_id=tid, describe=describe):
            if describe:
                described += 1
            activated += 1
    return activated


def _available_cache_key(user_id: str) -> str | None:
    org_id = resolve_org(user_id)
    if not org_id:
        return None
    return f"teams:available_channels:{org_id}"


def _invalidate_available_cache(user_id: str) -> None:
    try:
        key = _available_cache_key(user_id)
        if not key:
            return
        redis = get_redis_client()
        if redis:
            redis.delete(key)
    except Exception:
        logger.debug("[TeamsChannels] cache invalidation failed", exc_info=True)


def _fetch_all_workspace_channels(user_id: str) -> list[dict] | None:
    key = _available_cache_key(user_id)
    if key:
        try:
            redis = get_redis_client()
            if redis:
                cached = redis.get(key)
                if cached:
                    return json.loads(cached)
        except Exception:
            logger.debug("[TeamsChannels] cache read failed", exc_info=True)
    client = get_teams_client_for_user(user_id)
    if not client:
        return None
    hidden = channel_prefs.get_hidden_channel_ids(user_id, _PROVIDER)
    slim: list[dict] = []
    for team in client.list_joined_teams():
        team_id = team.get("id")
        team_name = team.get("displayName") or "team"
        if not team_id:
            continue
        for ch in client.list_team_channels(team_id):
            if len(slim) >= LIST_CHANNELS_CAP:
                break
            cid = ch.get("id")
            if not cid or cid in hidden:
                continue
            slim.append({
                "channel_id": cid,
                "team_id": team_id,
                "channel_name": ch.get("displayName"),
                "team_name": team_name,
                "is_private": ch.get("membershipType") == "private",
                "description": ch.get("description") or "",
            })
    if key and slim is not None:
        try:
            redis = get_redis_client()
            if redis:
                redis.setex(key, AVAILABLE_CHANNELS_CACHE_TTL, json.dumps(slim))
        except Exception:
            logger.debug("[TeamsChannels] cache write failed", exc_info=True)
    return slim


def _list_available_channels(user_id: str, member_ids: set[str]) -> list[dict]:
    all_ch = _fetch_all_workspace_channels(user_id)
    if not all_ch:
        return []
    available = []
    for ch in all_ch:
        cid = ch.get("channel_id")
        if not cid or cid in member_ids:
            continue
        name = ch.get("channel_name") or ""
        desc = ch.get("description") or ""
        channel_type, platform = _classify_channel(name, desc)
        available.append({
            "channel_id": cid,
            "team_id": ch.get("team_id"),
            "channel_name": name,
            "team_name": ch.get("team_name"),
            "is_private": ch.get("is_private", False),
            "is_member": False,
            "channel_type": channel_type,
            "detected_platform": platform,
            "metadata_summary": None,
            "metadata_status": "skipped",
            "is_dismissed": True,
        })
    return available


def _get_card_channel_id(user_id: str) -> str | None:
    return channel_prefs.get_incidents_channel_id(user_id, _PROVIDER)


def _set_card_channel(user_id: str, channel_id: str) -> None:
    channel_prefs.set_incidents_channel(user_id, _PROVIDER, channel_id)


def _clear_card_channel(user_id: str) -> None:
    channel_prefs.clear_incidents_channel(user_id, _PROVIDER)


def _is_active_channel(user_id: str, channel_id: str) -> bool:
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:isactive]")
            cur.execute(
                """SELECT 1 FROM slack_channels
                    WHERE provider = %s AND channel_id = %s AND is_member
                    LIMIT 1""",
                (_PROVIDER, channel_id),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


@teams_channels_bp.route("/channels", methods=["GET"])
@require_permission("connectors", "read")
def get_teams_channels(user_id):
    live = request.args.get("live", "1").lower() not in ("0", "false", "no")
    try:
        if live:
            try:
                auto_register_channels(user_id)
            except Exception:
                logger.warning("[TeamsChannels] reconcile on load failed", exc_info=True)

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:list]")
            cur.execute(
                f"""SELECT DISTINCT ON (channel_id)
                          channel_id, channel_name, is_private, is_member,
                          channel_type, detected_platform,
                          metadata_summary, metadata_status, team_id
                     FROM slack_channels
                    WHERE provider = %s AND {predicate}
                    ORDER BY channel_id, updated_at DESC""",
                (_PROVIDER, *pred_params),
            )
            rows = cur.fetchall()

        connected = []
        member_ids: set[str] = set()
        for r in rows:
            member_ids.add(r[0])
            connected.append({
                "channel_id": r[0],
                "channel_name": r[1],
                "is_private": r[2],
                "is_member": r[3],
                "channel_type": r[4],
                "detected_platform": r[5],
                "metadata_summary": r[6],
                "metadata_status": r[7],
                "team_id": r[8],
                "is_dismissed": False,
            })

        dismissed = _list_available_channels(user_id, member_ids) if live else []
        return jsonify({
            "connected": connected,
            "dismissed": dismissed,
            "card_channel_id": _get_card_channel_id(user_id),
        })
    except Exception:
        logger.exception("[TeamsChannels] list failed")
        return jsonify({"error": "Failed to get Teams channels"}), 500


@teams_channels_bp.route("/channels/refresh", methods=["POST"])
@require_permission("connectors", "write")
def refresh_teams_channels(user_id):
    try:
        _invalidate_available_cache(user_id)
        described = auto_register_channels(user_id)
        return jsonify({"message": "Channels refreshed", "described": described})
    except Exception:
        logger.exception("[TeamsChannels] refresh failed")
        return jsonify({"error": "Failed to refresh channels"}), 500


@teams_channels_bp.route("/channels", methods=["DELETE"])
@require_permission("connectors", "write")
def clear_teams_channels(user_id):
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:clear]")
            cur.execute("DELETE FROM slack_channels WHERE provider = %s", (_PROVIDER,))
            conn.commit()
        return jsonify({"message": "All Teams channels cleared"})
    except Exception:
        logger.exception("[TeamsChannels] clear failed")
        return jsonify({"error": "Failed to clear Teams channels"}), 500


@teams_channels_bp.route("/channels/<channel_id>/dismiss", methods=["POST"])
@require_permission("connectors", "write")
def dismiss_teams_channel(user_id, channel_id):
    # Graph has no bot-leave — hide locally and drop the active row.
    if _get_card_channel_id(user_id) == channel_id:
        try:
            _clear_card_channel(user_id)
        except Exception:
            logger.warning("[TeamsChannels] failed to clear card on dismiss", exc_info=True)

    channel_prefs.add_hidden_channel(user_id, _PROVIDER, channel_id)
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsChannels:dismiss]")
            cur.execute(
                """DELETE FROM slack_channels WHERE provider = %s AND channel_id = %s""",
                (_PROVIDER, channel_id),
            )
            conn.commit()
    except Exception:
        logger.exception("[TeamsChannels] dismiss failed for %s", sanitize(channel_id))
        return jsonify({"error": "Failed to deactivate channel"}), 500

    _invalidate_available_cache(user_id)
    return jsonify({"channel_id": channel_id, "is_dismissed": True})


@teams_channels_bp.route("/channels/<channel_id>/restore", methods=["POST"])
@require_permission("connectors", "write")
def restore_teams_channel(user_id, channel_id):
    team_id = (request.get_json(silent=True) or {}).get("team_id") or _team_id_for_channel(user_id, channel_id)
    if not team_id:
        return jsonify({
            "error": "Could not resolve team for this channel. Pass team_id in the body.",
            "code": "team_unknown",
        }), 400
    register_single_channel(user_id, channel_id, team_id=team_id)
    return jsonify({"channel_id": channel_id, "is_dismissed": False})


@teams_channels_bp.route("/channels/<channel_id>/metadata", methods=["PUT"])
@require_permission("connectors", "write")
def update_teams_channel_metadata(user_id, channel_id):
    summary = (request.get_json(silent=True) or {}).get("metadata_summary")
    if summary is None:
        return jsonify({"error": "metadata_summary is required"}), 400
    err = update_one_channel(
        user_id, _PROVIDER, channel_id,
        "metadata_summary = %s, metadata_status = 'ready'", (summary,),
    )
    return err or jsonify({"message": "Metadata updated"})


@teams_channels_bp.route("/channels/card-channel", methods=["GET", "PUT"])
@require_permission("connectors", "write")
def teams_card_channel(user_id):
    if request.method == "GET":
        return jsonify({"card_channel_id": _get_card_channel_id(user_id)})

    channel_id = (request.get_json(silent=True) or {}).get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        return jsonify({"error": "channel_id is required"}), 400
    if not _is_active_channel(user_id, channel_id):
        return jsonify({
            "error": "The incident card channel must be an active channel. Activate it first.",
            "code": "not_active",
        }), 409
    try:
        _set_card_channel(user_id, channel_id)
    except Exception:
        logger.exception("[TeamsChannels] set card channel failed")
        return jsonify({"error": "Failed to set card channel"}), 500
    return jsonify({"card_channel_id": channel_id})


@teams_channels_bp.route("/channels/metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_teams_metadata(user_id):
    channel_id = (request.get_json(silent=True) or {}).get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400
    err = update_one_channel(user_id, _PROVIDER, channel_id, "metadata_status = 'pending'", ())
    if err:
        return err
    _enqueue_metadata(user_id, channel_id, _team_id_for_channel(user_id, channel_id))
    return jsonify({"message": "Metadata generation started"})


@teams_channels_bp.route("/channels/activate", methods=["POST"])
@require_permission("connectors", "write")
def activate_teams_channels(user_id):
    channel_ids = (request.get_json(silent=True) or {}).get("channel_ids")
    if not isinstance(channel_ids, list) or not channel_ids:
        return jsonify({"error": "channel_ids (non-empty list) is required"}), 400
    if not all(isinstance(cid, str) and cid.strip() for cid in channel_ids):
        return jsonify({"error": "channel_ids must all be non-empty strings"}), 400
    channel_ids = list(dict.fromkeys(cid.strip() for cid in channel_ids))
    try:
        from routes.teams.teams_channel_metadata import bulk_activate_channels_task

        bulk_activate_channels_task.delay(user_id, channel_ids)
    except Exception:
        logger.exception("[TeamsChannels] enqueue activate failed")
        return jsonify({"error": "Failed to activate channels"}), 500
    return jsonify({"message": "Activation started", "queued": len(channel_ids)})
