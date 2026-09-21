"""
Slack channel management endpoints.

Mirrors :mod:`routes.github.github_repo_selection`: Aurora keeps a list of the
Slack channels it can see in the ``slack_channels`` table, each with an
LLM-generated (and user/agent-editable) description in ``metadata_summary``.
The agent queries these descriptions to decide which channel(s) are relevant
for a given incident/notification.

Aurora auto-registers every visible channel for *awareness* (see
``auto_register_channels``), but only **describes** the channels it's actually a
member of — the teammate model: it engages where it's been invited. Non-member
channels are registered as an aware, name-only index (``metadata_status =
'skipped'``); a user can promote one to described/routable at any time via the
metadata/generate route. Users curate by *dismissing* irrelevant channels
rather than picking relevant ones. This keeps LLM cost proportional to the
channels the org cares about, so it scales to workspaces with thousands of
channels.

Endpoints (all behind ``connectors`` RBAC):

    GET    /slack/channels                       -> stored channels {connected, dismissed}
    POST   /slack/channels/refresh               -> re-scan: add new & prune deleted channels
    DELETE /slack/channels                       -> forget all channels
    POST   /slack/channels/<id>/dismiss          -> hide a channel from routing (stays in Slack)
    POST   /slack/channels/<id>/restore          -> un-dismiss a channel
    PUT    /slack/channels/<id>/metadata         -> human-edit a channel description
    PUT    /slack/channels/<id>/notify           -> toggle notify_enabled for a channel
    POST   /slack/channels/metadata/generate     -> (re)generate a channel description
    POST   /slack/channels/activate             -> bulk-activate (describe+route) many channels
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

# Hard cap on how many channels we enumerate from Slack in one pass. Seeing fewer
# than this means we paged through the whole workspace, so a stored channel that's
# absent can be safely pruned; hitting it means the list is partial (don't prune).
LIST_CHANNELS_CAP = 2000

# Cap on channels a single bulk-activate request may queue, so a runaway request
# can't flood the metadata task queue.
MAX_BULK_ACTIVATE = 200


# Word-boundary patterns for incident-platform detection. Using anchored regex
# (rather than a bare substring `in` check) both avoids matching a platform
# name embedded in an unrelated token and clears CodeQL's "incomplete URL
# substring sanitization" rule, which flags substring checks on URL-like text.
_PLATFORM_PATTERNS = (
    ("incident.io", re.compile(r"\bincident\.io\b|\bincidentio\b")),
    ("pagerduty", re.compile(r"\bpagerduty\b|\bpd-incident\b")),
    ("opsgenie", re.compile(r"\bopsgenie\b")),
)

# Channel-name heuristics. Anchored on the left (token start) so we match
# "incident"/"alert" as words/prefixes, not an arbitrary substring mid-token
# (also clears CodeQL's URL-substring sanitization rule). Right side is loose so
# "alerts"/"oncall-db"/"incident-42" still match.
_INCIDENT_NAME_RE = re.compile(r"(?:^|[\s\-_])inc(?:ident)?(?:[\s\-_]|$)|\bincident")
_TEAM_NAME_RE = re.compile(r"\balert|\bon-?call|\bsev(?:[\s\-_]|$)")


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

    # Incident channels: platform-created OR named like one. Anchored patterns
    # (not bare `in name`) both read as intent and clear CodeQL's URL-substring
    # rule, which flags substring membership on URL-like text.
    if platform or _INCIDENT_NAME_RE.search(name):
        return "incident", platform
    # Alerting/on-call channels are still team-facing routing targets.
    if _TEAM_NAME_RE.search(name):
        return "team", platform
    return "general", platform


def _upsert_channel(cur, user_id: str, org_id: str | None, ch: dict,
                    existing: dict, initial_status: str = "pending") -> tuple[str | None, bool]:
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
            bool(ch.get("notify_enabled", False)),
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


def _update_one_channel(user_id: str, channel_id: str, set_clause: str, params: tuple):
    """Run a single-row ``UPDATE slack_channels ... WHERE channel_id`` and return
    a JSON response. Centralises the RLS + 404 + commit boilerplate shared by the
    dismiss/restore/metadata/notify routes.

    ``set_clause`` is the ``SET`` body (without ``updated_at``, which is always
    appended); ``params`` are its bind values, followed here by ``channel_id``.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:update]")
                cur.execute(
                    f"""UPDATE slack_channels SET {set_clause}, updated_at = NOW()
                        WHERE provider = 'slack' AND channel_id = %s""",
                    (*params, channel_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"error": "Channel not found"}), 404
                conn.commit()
        return None  # success — caller supplies the body
    except Exception:
        logger.exception("Error updating Slack channel %s", sanitize(channel_id))
        return jsonify({"error": "Failed to update channel"}), 500


def auto_register_channels(user_id: str, team_id: str | None = None,
                           describe_limit: int = MAX_AUTO_CHANNELS) -> int:
    """Auto-register the workspace's channels on connect (membership model).

    Registers ALL visible channels so Aurora is *aware* of every channel (the
    agent can still search them by name via ``list_slack_channels`` even without
    a description), but only **describes** the channels Aurora is actually a
    member of — the teammate model: it engages where it's been invited, not
    across the whole workspace. This keeps LLM cost proportional to the channels
    the org cares about (a handful) rather than workspace size (thousands).

    Two enumerations, deliberately separate:

    * Member channels via ``users.conversations`` — small, bounded, and paged
      to completion (no cap). This is the authoritative "active set" and is
      never truncated, so a joined channel can never be lost to the safety cap.
    * All visible channels via ``conversations.list`` — capped at
      ``LIST_CHANNELS_CAP`` for the searchable index. Hitting the cap means the
      index is partial (pruning is then skipped so we don't delete real rows).

    ``describe_limit`` still bounds how many member channels get an eager
    description, in case a workspace has an unusually large membership. Non-member
    channels register as ``metadata_status='skipped'`` until a user (or the agent)
    explicitly activates one. Dismissed channels are never resurfaced; channels
    Slack no longer lists are pruned when the full enumeration is complete (Slack
    itself is never modified). Best-effort; returns the number of descriptions
    enqueued. Idempotent.
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return 0
        # Member channels: complete (no cap) — the set we actually describe. A
        # joined channel must never be dropped by the index cap, so fetch it from
        # the membership API independently.
        member_channels = client.list_bot_channels()
        # Full workspace list for awareness/search. Own the cap here so we can
        # tell a complete enumeration (safe to prune stale rows) from a truncated
        # one (must NOT prune — we'd delete real channels we didn't page through).
        all_channels = client.list_all_channels(max_channels=LIST_CHANNELS_CAP)
    except Exception:
        logger.warning("[slack_channels] auto-register: failed to list channels", exc_info=True)
        return 0

    if not all_channels and not member_channels:
        return 0

    # Membership is authoritative: users.conversations only returns channels the
    # bot is in, so force is_member=True for those ids regardless of what the
    # (sometimes stale) conversations.list payload says.
    member_ids = {c.get("id") for c in member_channels if c.get("id")}

    # Merge both sources by id (member set wins on conflict so is_member sticks).
    by_id: dict[str, dict] = {}
    for c in all_channels:
        cid = c.get("id")
        if cid:
            by_id[cid] = c
    for c in member_channels:
        cid = c.get("id")
        if cid:
            by_id[cid] = {**by_id.get(cid, {}), **c, "is_member": True}
    channels = list(by_id.values())

    # Only reconcile deletions when we saw the whole workspace. Hitting the cap
    # means the list is partial, so absence doesn't imply the channel is gone.
    # (Member channels can't be over the cap — they're fetched completely.)
    enumeration_complete = len(all_channels) < LIST_CHANNELS_CAP
    live_ids = {c.get("id") for c in channels if c.get("id")}

    # Derive team_id from stored creds when the caller didn't supply it (e.g. the
    # manual "refresh" path), so rows are tagged consistently with the OAuth path.
    if not team_id:
        try:
            from utils.auth.stateless_auth import get_credentials_from_db
            creds = get_credentials_from_db(user_id, "slack") or {}
            team_id = creds.get("team_id")
        except Exception:
            team_id = None

    # We describe member channels (ranked recency-first, bounded by describe_limit
    # as a safety valve); everything else registers as 'skipped' for awareness.
    ranked = _rank_channels(channels)
    ranked_members = [c for c in ranked if c.get("id") in member_ids]
    describe_ids = {c.get("id") for c in ranked_members[:describe_limit]}

    org_id = resolve_org(user_id)
    newly_added_to_describe: list[str] = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:auto]")
                cur.execute(
                    "SELECT channel_id, user_id, is_dismissed FROM slack_channels WHERE provider = 'slack'"
                )
                existing = {}
                dismissed_ids = set()
                for r in cur.fetchall():
                    existing[r[0]] = r[1]
                    if r[2]:
                        dismissed_ids.add(r[0])
                for ch in ranked:
                    ch = {**ch, "channel_id": ch.get("id"), "channel_name": ch.get("name"),
                          "team_id": team_id}
                    # Skip channels the user dismissed — never resurrect them via
                    # auto-register/refresh (existing dismissed rows are left as-is).
                    if ch["channel_id"] in dismissed_ids:
                        continue
                    # Describe only channels Aurora is a member of; register the
                    # rest for awareness with 'skipped' so the UI doesn't show a
                    # perpetual "generating" spinner for them.
                    will_describe = ch["channel_id"] in describe_ids
                    _cid, is_new = _upsert_channel(
                        cur, user_id, org_id, ch, existing,
                        initial_status="pending" if will_describe else "skipped",
                    )
                    if is_new and _cid and will_describe:
                        newly_added_to_describe.append(_cid)

                # Reconcile deletions: rows we have for channels Slack no longer
                # lists (deleted/archived, or a private channel Aurora was removed
                # from) are pruned so Aurora doesn't route to dead channels. Only
                # when the enumeration was complete (see above). Never touches
                # Slack — this deletes Aurora's own rows only.
                if enumeration_complete and existing:
                    stale_ids = [cid for cid in existing if cid not in live_ids]
                    if stale_ids:
                        cur.execute(
                            """DELETE FROM slack_channels
                               WHERE provider = 'slack' AND channel_id = ANY(%s)""",
                            (stale_ids,),
                        )
                        logger.info("[slack_channels] pruned %d channel(s) no longer in Slack",
                                    len(stale_ids))
                conn.commit()
    except Exception:
        logger.warning("[slack_channels] auto-register: DB upsert failed", exc_info=True)
        return 0

    for channel_id in newly_added_to_describe:
        _enqueue_metadata(user_id, channel_id)
    logger.info(
        "[slack_channels] auto-registered %d channel(s) (%d member), describing %d",
        len(ranked), len(member_ids), len(newly_added_to_describe),
    )
    return len(newly_added_to_describe)


def register_single_channel(user_id: str, channel_id: str,
                            team_id: str | None = None) -> bool:
    """Register (and describe) one channel Aurora was just added to.

    Lightweight counterpart to auto_register_channels for the
    ``member_joined_channel`` event (e.g. Aurora invited to an incident.io
    channel). Fetches just that channel's info, upserts it, and enqueues a
    description. Idempotent. A re-invite is treated as the latest signal: a
    channel the user previously dismissed is restored (un-dismissed) and
    re-described. Returns True if a new row was created.
    """
    if not channel_id:
        return False
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return False
        info = client.get_channel_info(channel_id) or {}
    except Exception:
        logger.warning("[slack_channels] single-register: failed to fetch channel info", exc_info=True)
        return False

    ch = {**info, "channel_id": channel_id, "channel_name": info.get("name"),
          "team_id": team_id}

    org_id = resolve_org(user_id)
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:joined]")
                cur.execute(
                    """SELECT user_id, is_dismissed FROM slack_channels
                       WHERE provider = 'slack' AND channel_id = %s""",
                    (channel_id,),
                )
                row = cur.fetchone()
                # A re-invite is the latest signal — un-dismiss so the channel
                # comes back (we always go by the most recent event, not history).
                was_dismissed = bool(row and row[1])
                if was_dismissed:
                    cur.execute(
                        """UPDATE slack_channels SET is_dismissed = FALSE, updated_at = NOW()
                           WHERE provider = 'slack' AND channel_id = %s""",
                        (channel_id,),
                    )
                    logger.info("[slack_channels] re-invited to dismissed channel %s; restoring",
                                sanitize(channel_id))
                existing = {channel_id: row[0]} if row else {}
                _cid, is_new = _upsert_channel(cur, user_id, org_id, ch, existing,
                                               initial_status="pending")
                conn.commit()
    except Exception:
        logger.warning("[slack_channels] single-register: DB upsert failed", exc_info=True)
        return False

    # Describe newly-joined AND freshly-restored channels — a channel Aurora was
    # explicitly (re-)invited to is relevant by definition, so it's worth the
    # cheap LLM call. Restored rows aren't "new" but should refresh their blurb.
    if (is_new or was_dismissed) and _cid:
        _enqueue_metadata(user_id, channel_id)
        logger.info("[slack_channels] registered joined channel %s", sanitize(channel_id))
    return bool(is_new)


@slack_channels_bp.route("/channels", methods=["GET"])
@require_permission("connectors", "read")
def get_slack_channels(user_id):
    """Return the org's stored channels split into {connected, dismissed}."""
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
                              metadata_summary, metadata_status, is_dismissed
                         FROM slack_channels
                        WHERE provider = 'slack' AND {predicate}
                        ORDER BY channel_id, updated_at DESC""",
                    pred_params,
                )
                rows = cur.fetchall()

        # Split active vs. dismissed so the UI can show them separately.
        connected = []
        dismissed = []
        for r in rows:
            entry = {
                "channel_id": r[0],
                "channel_name": r[1],
                "is_private": r[2],
                "is_member": r[3],
                "channel_type": r[4],
                "detected_platform": r[5],
                "notify_enabled": r[6],
                "metadata_summary": r[7],
                "metadata_status": r[8],
                "is_dismissed": r[9],
            }
            if r[9]:
                dismissed.append(entry)
            else:
                connected.append(entry)

        return jsonify({"connected": connected, "dismissed": dismissed})
    except Exception:
        logger.exception("Error getting Slack channels")
        return jsonify({"error": "Failed to get Slack channels"}), 500


@slack_channels_bp.route("/channels/<channel_id>/dismiss", methods=["POST"])
@require_permission("connectors", "write")
def dismiss_slack_channel(user_id, channel_id):
    """Mark a channel as dismissed (irrelevant).

    Does NOT touch Slack — Aurora stays in the channel. It just stops routing/
    notifying there, hides it from the main list, and prevents auto-register/
    refresh from resurfacing it. Reversible via the restore route.
    """
    err = _update_one_channel(
        user_id, channel_id, "is_dismissed = TRUE, notify_enabled = FALSE", ()
    )
    return err or jsonify({"channel_id": channel_id, "is_dismissed": True})


@slack_channels_bp.route("/channels/<channel_id>/restore", methods=["POST"])
@require_permission("connectors", "write")
def restore_slack_channel(user_id, channel_id):
    """Un-dismiss a channel so Aurora is aware of it again."""
    err = _update_one_channel(user_id, channel_id, "is_dismissed = FALSE", ())
    return err or jsonify({"channel_id": channel_id, "is_dismissed": False})


@slack_channels_bp.route("/channels/refresh", methods=["POST"])
@require_permission("connectors", "write")
def refresh_slack_channels(user_id):
    """Re-scan the workspace and register any channels Aurora can now see.

    For workspaces connected before auto-registration existed (or when new
    channels have appeared), this brings the stored channel list up to date
    without requiring a reconnect. Idempotent: existing rows/descriptions are
    preserved and dismissed channels stay dismissed. New channels are added (the
    most recent also get a description); channels Slack no longer lists
    (deleted/archived) are pruned from Aurora's table (Slack is never modified).
    """
    try:
        described = auto_register_channels(user_id)
        return jsonify({"message": "Channels refreshed", "described": described})
    except Exception:
        logger.exception("Error refreshing Slack channels")
        return jsonify({"error": "Failed to refresh channels"}), 500


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
    summary = (request.get_json(silent=True) or {}).get("metadata_summary")
    if summary is None:
        return jsonify({"error": "metadata_summary is required"}), 400
    err = _update_one_channel(
        user_id, channel_id,
        "metadata_summary = %s, metadata_status = 'ready'", (summary,),
    )
    return err or jsonify({"message": "Metadata updated"})


@slack_channels_bp.route("/channels/<channel_id>/notify", methods=["PUT"])
@require_permission("connectors", "write")
def update_channel_notify(user_id, channel_id):
    """Toggle whether Aurora may proactively notify a channel."""
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    err = _update_one_channel(user_id, channel_id, "notify_enabled = %s", (enabled,))
    return err or jsonify({"channel_id": channel_id, "notify_enabled": enabled})


@slack_channels_bp.route("/channels/metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_channel_metadata(user_id):
    """(Re)generate the LLM description for a specific channel."""
    channel_id = (request.get_json(silent=True) or {}).get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id is required"}), 400
    # Flip to 'generating' first so the UI shows a spinner; 404 if unknown.
    err = _update_one_channel(user_id, channel_id, "metadata_status = 'generating'", ())
    if err:
        return err
    _enqueue_metadata(user_id, channel_id)
    return jsonify({"message": "Metadata generation started"})


@slack_channels_bp.route("/channels/activate", methods=["POST"])
@require_permission("connectors", "write")
def activate_slack_channels(user_id):
    """Bulk-activate channels: describe + make them routable.

    Powers the "search a keyword (e.g. 'oncall'), check the matches, activate"
    flow for large workspaces where Aurora is a member of only a few channels
    but the org wants a set of channels routable without inviting the bot to
    each. Flips each id to ``metadata_status='generating'`` and enqueues a
    description (idempotent — re-activating just regenerates). Unknown/dismissed
    ids are skipped silently. Returns the number of channels queued.
    """
    channel_ids = (request.get_json(silent=True) or {}).get("channel_ids")
    if not isinstance(channel_ids, list) or not channel_ids:
        return jsonify({"error": "channel_ids (non-empty list) is required"}), 400
    # Guard against an unbounded request flooding the metadata queue.
    if len(channel_ids) > MAX_BULK_ACTIVATE:
        return jsonify({"error": f"Too many channels; activate at most {MAX_BULK_ACTIVATE} at once"}), 400

    try:
        activated = _activate_channels(user_id, channel_ids)
    except Exception:
        logger.exception("Error activating Slack channels")
        return jsonify({"error": "Failed to activate channels"}), 500

    return jsonify({"message": "Activation started", "activated": activated})


def _activate_channels(user_id: str, channel_ids: list[str]) -> int:
    """Flip the given (non-dismissed, existing) channels to 'generating' and
    enqueue a description for each. Returns how many were actually activated.

    Only rows that exist and aren't dismissed are touched (activating a dismissed
    channel would contradict the user's dismissal); ``RETURNING`` tells us which
    ids were real so we enqueue exactly those.
    """
    activated: list[str] = []
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:activate]")
            cur.execute(
                """UPDATE slack_channels
                      SET metadata_status = 'generating', updated_at = NOW()
                    WHERE provider = 'slack'
                      AND is_dismissed = FALSE
                      AND channel_id = ANY(%s)
                RETURNING channel_id""",
                (channel_ids,),
            )
            activated = [r[0] for r in cur.fetchall()]
            conn.commit()

    for channel_id in activated:
        _enqueue_metadata(user_id, channel_id)
    return len(activated)


def _enqueue_metadata(user_id: str, channel_id: str):
    """Enqueue the description task; mark 'error' on the row if enqueue fails."""
    try:
        from routes.slack.slack_channel_metadata import generate_channel_metadata
        generate_channel_metadata.delay(user_id, channel_id)
    except Exception as e:
        logger.warning("Failed to enqueue channel metadata for %s: %s", sanitize(channel_id), sanitize(e))
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
