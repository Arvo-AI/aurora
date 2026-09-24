"""
Slack channel management endpoints.

Mirrors :mod:`routes.github.github_repo_selection`: Aurora keeps a list of the
Slack channels it can see in the ``slack_channels`` table, each with an
LLM-generated (and user/agent-editable) description in ``metadata_summary``.
The agent queries these descriptions to decide which channel(s) are relevant
for a given incident/notification.

**Membership is the single source of truth.** A channel is "active" iff Aurora
is a member of it in Slack. There is no separate "aware but not joined" tier and
no local "dismissed" flag that diverges from Slack — the two concepts collapse
into one: *is the bot in the channel or not?* This keeps the UI, the agent's
``list_slack_channels`` tool (which lists member channels), and Slack itself
always in agreement, and it scales to workspaces with thousands of channels
because Aurora only stores/describes the handful it has been invited to.

    * **Active**   — Aurora is a member (``is_member = TRUE``). It engages here:
      posts free-form teammate messages, and the agent can route to it.
    * **Inactive** — every other workspace channel. Aurora is not a member and
      stays silent. These are enumerated live from Slack on read (not persisted)
      so the "activate" picker always reflects the real workspace.

State transitions mirror Slack membership directly:
    * **Activate**   -> ``conversations.join`` (public channels). The bot becomes
      a member; the row flips to ``is_member = TRUE`` and gets a description.
    * **Deactivate** -> ``conversations.leave``. The bot leaves; the row is
      pruned so the channel falls back into the live Inactive list.
    * **Invited in Slack** (``member_joined_channel``) -> auto-registers as
      active (optional real-time hook).

The Active list is reconciled against real Slack membership on every load of
:func:`get_slack_channels` (not just via the Refresh button), so a channel
Aurora was removed from drops off Active — and a newly-joined one appears —
without needing a dedicated ``member_left_channel`` event.

Endpoints (all behind ``connectors`` RBAC):

    GET    /slack/channels                       -> {connected (members), dismissed (available-to-join), card_channel_id}
    POST   /slack/channels/refresh               -> reconcile the active list against real Slack membership
    DELETE /slack/channels                       -> forget all channels
    POST   /slack/channels/<id>/dismiss          -> deactivate: LEAVE the channel in Slack
    POST   /slack/channels/<id>/restore          -> reactivate: JOIN the channel in Slack
    PUT    /slack/channels/<id>/metadata         -> human-edit a channel description
    GET/PUT /slack/channels/card-channel         -> get/set the single incident-card channel
    POST   /slack/channels/metadata/generate     -> (re)generate a channel description
    POST   /slack/channels/activate              -> bulk-activate (join + describe) many channels

    * Card channel — exactly one active channel receives the structured incident
      card (the template). Stored as the ``slack_incidents_channel_id`` pref /
      creds field, not a per-channel flag.
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
                channel_data, metadata_status)
           VALUES (%s, %s, 'slack', %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    dismiss/restore/metadata routes.

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
    """Reconcile Aurora's stored channels against real Slack membership.

    Membership is the source of truth (see module docstring), so this persists
    exactly the channels Aurora is a member of and nothing else:

    * New member channels are inserted (``is_member = TRUE``) and described (up
      to ``describe_limit``, a safety valve for unusually large memberships).
    * Rows for channels Aurora is *no longer* a member of are pruned — they fall
      back into the live "Inactive / available-to-join" list on the next read.

    Only member channels are enumerated (``users.conversations``), which is
    small, bounded, and paged to completion — so a joined channel can never be
    lost to a safety cap, and we never enumerate the whole workspace here (that's
    done live, on read, in :func:`get_slack_channels`). Best-effort; returns the
    number of descriptions enqueued. Idempotent.
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return 0
        # Member channels only: this IS the active set. Complete (no cap) so a
        # joined channel is never dropped.
        member_channels = client.list_bot_channels()
    except Exception:
        logger.warning("[slack_channels] auto-register: failed to list channels", exc_info=True)
        return 0

    # Membership is authoritative and these all come from users.conversations,
    # so force is_member=True regardless of the (sometimes stale) payload flag.
    member_ids = {c.get("id") for c in member_channels if c.get("id")}

    # Derive team_id from stored creds when the caller didn't supply it (e.g. the
    # manual "refresh" path), so rows are tagged consistently with the OAuth path.
    if not team_id:
        try:
            from utils.auth.stateless_auth import get_credentials_from_db
            creds = get_credentials_from_db(user_id, "slack") or {}
            team_id = creds.get("team_id")
        except Exception:
            team_id = None

    # Describe member channels, recency-first, bounded by describe_limit.
    ranked_members = _rank_channels(member_channels)
    describe_ids = {c.get("id") for c in ranked_members[:describe_limit]}

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

                for ch in ranked_members:
                    ch = {**ch, "channel_id": ch.get("id"), "channel_name": ch.get("name"),
                          "team_id": team_id, "is_member": True}
                    will_describe = ch["channel_id"] in describe_ids
                    _cid, is_new = _upsert_channel(
                        cur, user_id, org_id, ch, existing,
                        initial_status="pending" if will_describe else "skipped",
                    )
                    if is_new and _cid and will_describe:
                        newly_added_to_describe.append(_cid)

                # Reconcile membership: any stored row that is NOT in the current
                # member set means Aurora left / was removed from that channel, so
                # prune it (Slack is never modified — this deletes Aurora's own
                # rows only). The channel reappears in the live Inactive list.
                if existing:
                    stale_ids = [cid for cid in existing if cid not in member_ids]
                    if stale_ids:
                        cur.execute(
                            """DELETE FROM slack_channels
                               WHERE provider = 'slack' AND channel_id = ANY(%s)""",
                            (stale_ids,),
                        )
                        logger.info("[slack_channels] pruned %d channel(s) Aurora is no longer in",
                                    len(stale_ids))
                conn.commit()
    except Exception:
        logger.warning("[slack_channels] auto-register: DB upsert failed", exc_info=True)
        return 0

    for channel_id in newly_added_to_describe:
        _enqueue_metadata(user_id, channel_id)
    logger.info(
        "[slack_channels] reconciled %d member channel(s), describing %d",
        len(member_ids), len(newly_added_to_describe),
    )
    return len(newly_added_to_describe)


def register_single_channel(user_id: str, channel_id: str,
                            team_id: str | None = None,
                            allow_restore: bool = True) -> bool:
    """Register (and describe) one channel Aurora was just added to.

    Lightweight counterpart to auto_register_channels for the
    ``member_joined_channel`` event (e.g. Aurora invited to an incident.io
    channel) and the activate/restore routes. Fetches just that channel's info,
    upserts it as an active member channel, and enqueues a description.
    Idempotent. Returns True if a new row was created.

    ``allow_restore`` is accepted for backwards-compatibility with callers but is
    now a no-op: membership is the source of truth, so there is no separate
    "dismissed" state to restore — a channel is active iff Aurora is a member.
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

    # Aurora is a member (it was just added / just joined), so mark it as such.
    ch = {**info, "channel_id": channel_id, "channel_name": info.get("name"),
          "team_id": team_id, "is_member": True}

    org_id = resolve_org(user_id)
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:joined]")
                cur.execute(
                    """SELECT user_id FROM slack_channels
                       WHERE provider = 'slack' AND channel_id = %s""",
                    (channel_id,),
                )
                row = cur.fetchone()
                existing = {channel_id: row[0]} if row else {}
                _cid, is_new = _upsert_channel(cur, user_id, org_id, ch, existing,
                                               initial_status="pending")
                conn.commit()
    except Exception:
        logger.warning("[slack_channels] single-register: DB upsert failed", exc_info=True)
        return False

    # Describe newly-joined channels — a channel Aurora was explicitly invited to
    # (or just joined) is relevant by definition, so it's worth the cheap LLM
    # call. Idempotent re-registers of an existing member channel skip this.
    if is_new and _cid:
        _enqueue_metadata(user_id, channel_id)
        logger.info("[slack_channels] registered joined channel %s", sanitize(channel_id))
    return bool(is_new)


@slack_channels_bp.route("/channels", methods=["GET"])
@require_permission("connectors", "read")
def get_slack_channels(user_id):
    """Return the org's channels split into {connected, dismissed}.

    Membership is the source of truth:
      * ``connected`` — channels Aurora is a member of (the Active list). These
        are the stored rows, which carry the LLM description + status.
      * ``dismissed`` — every *other* channel in the workspace, enumerated live
        from Slack (not persisted). This is the "Inactive / available-to-join"
        picker; activating one makes Aurora join it. Named ``dismissed`` purely
        to preserve the existing response contract the frontend consumes.

    Membership is reconciled against Slack on every load (not just via the
    Refresh button): we re-list the bot's member channels, prune rows Aurora is
    no longer in, and register newly-joined ones — so the Active list is always
    accurate even without the member_joined/left_channel events. Best-effort:
    if the reconcile call fails we still serve whatever is stored.

    Also returns ``card_channel_id`` — the single channel that receives the
    structured incident card.
    """
    try:
        # Reconcile stored rows against live Slack membership before reading, so a
        # channel Aurora was removed from drops off Active (and a newly-joined one
        # appears) without waiting for a manual refresh. Best-effort.
        try:
            auto_register_channels(user_id)
        except Exception:
            logger.warning("[slack_channels] membership reconcile on load failed", exc_info=True)

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        # Stored rows = the channels Aurora is a member of (the active set).
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:list]")
                cur.execute(
                    f"""SELECT DISTINCT ON (channel_id)
                              channel_id, channel_name, is_private, is_member,
                              channel_type, detected_platform,
                              metadata_summary, metadata_status
                         FROM slack_channels
                        WHERE provider = 'slack' AND {predicate}
                        ORDER BY channel_id, updated_at DESC""",
                    pred_params,
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
                "is_dismissed": False,
            })

        # The Inactive list is derived live from Slack (never persisted): every
        # workspace channel Aurora is NOT already a member of. Marked
        # is_dismissed=True so the existing frontend slots them into its
        # "Inactive / activate" section unchanged. Best-effort — if Slack can't
        # be reached we just return an empty available list rather than failing.
        dismissed = _list_available_channels(user_id, member_ids)

        return jsonify({
            "connected": connected,
            "dismissed": dismissed,
            "card_channel_id": _get_card_channel_id(user_id),
        })
    except Exception:
        logger.exception("Error getting Slack channels")
        return jsonify({"error": "Failed to get Slack channels"}), 500


def _list_available_channels(user_id: str, member_ids: set[str]) -> list[dict]:
    """Live-list workspace channels Aurora is NOT a member of (the Inactive set).

    Not persisted: this is computed on every read so the "activate" picker always
    reflects the real workspace. Returns entries shaped like the stored rows (with
    is_dismissed=True) so the frontend can render them in the same list.
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return []
        all_channels = client.list_all_channels(max_channels=LIST_CHANNELS_CAP)
    except Exception:
        logger.warning("[slack_channels] could not list available channels", exc_info=True)
        return []

    available = []
    for ch in all_channels:
        cid = ch.get("id")
        # Skip channels Aurora is already in — those are the Active list.
        if not cid or cid in member_ids:
            continue
        channel_type, platform = _classify_channel(ch)
        available.append({
            "channel_id": cid,
            "channel_name": ch.get("name"),
            "is_private": ch.get("is_private", False),
            "is_member": False,
            "channel_type": channel_type,
            "detected_platform": platform,
            "metadata_summary": None,
            "metadata_status": "skipped",
            "is_dismissed": True,
        })
    return available


@slack_channels_bp.route("/channels/<channel_id>/dismiss", methods=["POST"])
@require_permission("connectors", "write")
def dismiss_slack_channel(user_id, channel_id):
    """Deactivate a channel: Aurora LEAVES it in Slack.

    Membership is the source of truth, so deactivating actually removes the bot
    from the channel (``conversations.leave``) and prunes the stored row. The
    channel then reappears in the live "Inactive / available-to-join" list. This
    is visible to everyone in the channel (Slack posts a "left the channel"
    system message).

    If the dismissed channel was the incident card channel, its card designation
    is cleared too — the card then has no destination until the user picks a new
    one (the UI warns before this happens).
    """
    # Clear the card designation first so we never leave it pointing at a channel
    # Aurora is no longer in (which would silently drop the card).
    if _get_card_channel_id(user_id) == channel_id:
        try:
            _clear_card_channel(user_id)
        except Exception:
            logger.warning("Failed to clear card channel on dismiss", exc_info=True)

    # Leave the channel in Slack (best-effort — a failure to leave, e.g. already
    # not a member, shouldn't block removing our local row).
    try:
        client = get_slack_client_for_user(user_id)
        if client:
            client.leave_channel(channel_id)
    except Exception:
        logger.warning("[slack_channels] failed to leave channel on deactivate", exc_info=True)

    # Prune the row: it's no longer a member channel, so it belongs in the live
    # Inactive list, not the stored Active set.
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:deactivate]")
                cur.execute(
                    """DELETE FROM slack_channels
                        WHERE provider = 'slack' AND channel_id = %s""",
                    (channel_id,),
                )
                conn.commit()
    except Exception:
        logger.exception("Error deactivating Slack channel %s", sanitize(channel_id))
        return jsonify({"error": "Failed to deactivate channel"}), 500

    return jsonify({"channel_id": channel_id, "is_dismissed": True})


@slack_channels_bp.route("/channels/<channel_id>/restore", methods=["POST"])
@require_permission("connectors", "write")
def restore_slack_channel(user_id, channel_id):
    """Reactivate a channel: Aurora JOINS it in Slack.

    The inverse of deactivate. Joins the (public) channel via
    ``conversations.join``, registers + describes it. Private channels can't be
    self-joined, so this returns a 409 telling the user to invite Aurora
    manually (the ``member_joined_channel`` event then activates it).
    """
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return jsonify({"error": "Slack not connected"}), 400
        joined = client.join_channel(channel_id)
    except Exception:
        logger.exception("Error joining Slack channel %s", sanitize(channel_id))
        return jsonify({"error": "Failed to activate channel"}), 500

    # Join failed — almost always a private channel that needs a human invite.
    if not joined:
        return jsonify({
            "error": "Aurora couldn't join this channel automatically. If it's "
                     "private, invite Aurora to it in Slack and it will activate.",
            "code": "join_failed",
        }), 409

    # Joined — register + describe it as an active member channel.
    register_single_channel(user_id, channel_id)
    return jsonify({"channel_id": channel_id, "is_dismissed": False})


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


@slack_channels_bp.route("/channels/card-channel", methods=["GET", "PUT"])
@require_permission("connectors", "write")
def card_channel(user_id):
    """Get or set the single channel that receives the structured incident card.

    GET  -> {"card_channel_id": <id or null>}
    PUT  {"channel_id": <id>} -> designate that channel as the card channel.

    The card channel must be an ACTIVE channel (described, not dismissed): the
    card is the one structured template Aurora posts, and it only goes here.
    Every other active channel can still receive free-form teammate messages.
    Writing both the org preference and the stored creds field keeps the
    notification service's resolver (creds-first) in sync with the UI choice.
    """
    if request.method == "GET":
        return jsonify({"card_channel_id": _get_card_channel_id(user_id)})

    channel_id = (request.get_json(silent=True) or {}).get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        return jsonify({"error": "channel_id is required"}), 400

    # Must be an active channel — the card needs a live, engaged destination.
    if not _is_active_channel(user_id, channel_id):
        return jsonify({
            "error": "The incident card channel must be an active channel. "
                     "Activate it first, then set it as the card channel.",
            "code": "not_active",
        }), 409

    try:
        _set_card_channel(user_id, channel_id)
    except Exception:
        logger.exception("Error setting card channel")
        return jsonify({"error": "Failed to set card channel"}), 500
    return jsonify({"card_channel_id": channel_id})


def _get_card_channel_id(user_id: str) -> str | None:
    """Resolve the current incident card channel (creds-first, org-pref fallback),
    mirroring the notification service's resolver. Best-effort; None on error."""
    try:
        from utils.auth.stateless_auth import (
            get_credentials_from_db, get_org_id_for_user, get_org_preference,
        )
        creds = get_credentials_from_db(user_id, "slack") or {}
        if creds.get("incidents_channel_id"):
            return creds["incidents_channel_id"]
        org_id = get_org_id_for_user(user_id)
        if org_id:
            return get_org_preference(org_id, 'slack_incidents_channel_id') or None
    except Exception:
        logger.debug("Could not resolve card channel id", exc_info=True)
    return None


def _is_active_channel(user_id: str, channel_id: str) -> bool:
    """True if the channel is an active (member) channel Aurora engages with."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:isactive]")
                cur.execute(
                    """SELECT 1 FROM slack_channels
                        WHERE provider = 'slack' AND channel_id = %s
                          AND is_member
                        LIMIT 1""",
                    (channel_id,),
                )
                return cur.fetchone() is not None
    except Exception:
        logger.exception("Error checking active channel %s", sanitize(channel_id))
        return False


def _set_card_channel(user_id: str, channel_id: str) -> None:
    """Persist the card channel to both the org preference and stored creds so
    the notification resolver (which reads creds first) honors the UI choice."""
    from utils.auth.stateless_auth import get_org_id_for_user, store_org_preference
    from utils.auth.token_management import store_tokens_in_db
    from utils.auth.stateless_auth import get_credentials_from_db

    # Look up the channel name for a friendlier stored label (best-effort).
    channel_name = ""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[slack_channels:cardname]")
                cur.execute(
                    """SELECT channel_name FROM slack_channels
                        WHERE provider = 'slack' AND channel_id = %s LIMIT 1""",
                    (channel_id,),
                )
                row = cur.fetchone()
                channel_name = (row[0] if row else "") or ""
    except Exception:
        logger.debug("Could not read channel name for card channel", exc_info=True)

    org_id = get_org_id_for_user(user_id)
    if org_id:
        store_org_preference(org_id, 'slack_incidents_channel_id', channel_id)
        store_org_preference(org_id, 'slack_incidents_channel_name', channel_name)

    # Update the creds copy too — the resolver checks creds before the org pref,
    # so leaving a stale creds value would silently override the new choice.
    creds = get_credentials_from_db(user_id, "slack") or {}
    creds["incidents_channel_id"] = channel_id
    creds["incidents_channel_name"] = channel_name
    store_tokens_in_db(user_id, creds, "slack")


def _clear_card_channel(user_id: str) -> None:
    """Unset the incident card channel in both the org preference and creds.

    Called when the card channel is deactivated: the card then has no
    destination (the notification resolver returns None and the card is skipped)
    until the user picks a new one. Clears both stores so the creds-first
    resolver doesn't keep serving a stale value."""
    from utils.auth.stateless_auth import get_org_id_for_user, store_org_preference, get_credentials_from_db
    from utils.auth.token_management import store_tokens_in_db

    org_id = get_org_id_for_user(user_id)
    if org_id:
        store_org_preference(org_id, 'slack_incidents_channel_id', "")
        store_org_preference(org_id, 'slack_incidents_channel_name', "")

    creds = get_credentials_from_db(user_id, "slack") or {}
    if creds:
        creds.pop("incidents_channel_id", None)
        creds.pop("incidents_channel_name", None)
        store_tokens_in_db(user_id, creds, "slack")


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
    """Bulk-activate channels: Aurora JOINS each one and describes it.

    Powers the "search a keyword (e.g. 'oncall'), check the matches, activate"
    flow. Each id is joined via ``conversations.join`` (public channels), then
    registered as a member channel and described. Private channels can't be
    self-joined and are skipped (the user must invite Aurora). Idempotent —
    re-activating an already-member channel just re-describes it. Returns the
    number of channels successfully joined/activated.
    """
    channel_ids = (request.get_json(silent=True) or {}).get("channel_ids")
    if not isinstance(channel_ids, list) or not channel_ids:
        return jsonify({"error": "channel_ids (non-empty list) is required"}), 400
    # Every entry must be a non-empty string — otherwise psycopg2 adaptation of
    # ANY(%s) can fail at execute() and surface as a 500 instead of a 400.
    if not all(isinstance(cid, str) and cid.strip() for cid in channel_ids):
        return jsonify({"error": "channel_ids must all be non-empty strings"}), 400
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
    """Join each requested channel and register it as an active member channel.

    Membership is the source of truth, so "activate" means "join": Aurora joins
    the (public) channel via ``conversations.join`` and then registers +
    describes it. Private channels can't be self-joined and are skipped silently
    (they need a human invite). Returns how many were actually joined.
    """
    client = get_slack_client_for_user(user_id)
    if not client:
        logger.warning("[slack_channels] cannot activate channels: no Slack client for user")
        return 0

    joined: list[str] = []
    for channel_id in channel_ids:
        # join_channel swallows its own errors and returns None on failure
        # (e.g. private channel), so a skip here is expected and non-fatal.
        if client.join_channel(channel_id):
            joined.append(channel_id)

    # Register + describe each joined channel as an active member channel.
    for channel_id in joined:
        register_single_channel(user_id, channel_id)

    return len(joined)


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
