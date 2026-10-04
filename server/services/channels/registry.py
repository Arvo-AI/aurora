"""Provider-parameterised queries over the ``slack_channels`` table.

The table is shared by every chat platform and keyed by ``provider``; these are
the four queries a platform's agent tools and event handlers need and that the
Slack code paths now call. Contract: the Slack SQL must stay byte-identical in
substance to the pre-refactor inline queries (the Slack suites assert on its
substrings), and ``provider`` is always a bound parameter, never interpolated.
Add a platform by adding it to ``PROVIDERS`` (and a label format if the
platform's channels are not spelled ``#name``).

Everything else about the Slack channel lifecycle (reconcile, backfill claims,
activation) still lives in ``routes.slack.slack_channels`` and
``routes.slack.slack_channel_metadata``.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from utils.auth.stateless_auth import set_rls_context
from utils.db import org_scope
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

PROVIDERS = ("slack",)

# How a channel is spelled when the agent is told where a message came from.
_LABEL_FORMATS = {"slack": "#{name} ({id})"}
_DEFAULT_LABEL_FORMAT = "{name} ({id})"


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown channel provider: {provider!r}")
    return provider


def upsert_channel(cur, user_id: str, org_id: Optional[str], provider: str, ch: Dict[str, Any],
                   existing: Dict[str, str], *, channel_type: str,
                   detected_platform: Optional[str],
                   initial_status: str = "pending") -> Tuple[Optional[str], bool]:
    """Upsert one channel row. Returns (channel_id, was_newly_added).

    ``existing`` maps channel_id -> owner_user_id and is mutated in place so a
    channel already connected by another org member keeps its original owner on
    the conflict key (mirrors github save_repo_selections).

    ``initial_status`` is the metadata_status for a NEW row: 'pending' when a
    description will be generated, 'skipped' when the channel is registered for
    awareness but intentionally not described (bulk auto-register beyond the
    recency cap). Existing rows never have their status reset (ON CONFLICT does
    not touch metadata_status), so a prior 'ready' description is preserved.

    ``channel_type`` / ``detected_platform`` come from the provider's offline
    classifier (``services.channels.classify``) and are required so a provider
    cannot silently register every channel as ``general``.
    """
    _check_provider(provider)
    channel_id = ch.get("channel_id") or ch.get("id")
    if not channel_id:
        return None, False
    owner_id = existing.get(channel_id, user_id)
    cur.execute(
        """INSERT INTO slack_channels
               (user_id, org_id, provider, team_id, channel_id, channel_name,
                is_private, is_member, channel_type, detected_platform,
                channel_data, metadata_status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, provider, channel_id) DO UPDATE SET
               channel_name = EXCLUDED.channel_name,
               is_private = EXCLUDED.is_private,
               is_member = EXCLUDED.is_member,
               channel_type = EXCLUDED.channel_type,
               detected_platform = EXCLUDED.detected_platform,
               channel_data = EXCLUDED.channel_data,
               -- Don't bump updated_at for a row awaiting a description:
               -- auto_register upserts every member on each reconcile, and
               -- updated_at is "time since the description was queued", which the
               -- staleness checks use to spot a lost task. Bumping it would keep a
               -- stuck row looking fresh forever. 'generating' counts too: the
               -- sweep recovers one abandoned by a killed worker on the same
               -- window, and a page load must not reset its age.
               updated_at = CASE
                   WHEN slack_channels.metadata_status IN ('pending', 'generating')
                   THEN slack_channels.updated_at
                   ELSE NOW()
               END""",
        (
            owner_id, org_id, provider, ch.get("team_id"), channel_id,
            ch.get("channel_name") or ch.get("name"),
            ch.get("is_private", False), ch.get("is_member", False),
            channel_type, detected_platform,
            json.dumps(ch),
            initial_status,
        ),
    )
    is_new = channel_id not in existing
    if is_new:
        existing[channel_id] = user_id
    return channel_id, is_new


def get_connected_channels(user_id: str, provider: str) -> List[Dict[str, Any]]:
    """The ACTIVE, described channels Aurora may post teammate messages to — the
    routing-decision source. Only channels Aurora is a member of (membership is
    the source of truth) that have been described (``metadata_status='ready'``),
    so the agent routes only to channels it's actually in. Raises on DB error so
    tool callers can report it."""
    _check_provider(provider)
    org_id = org_scope.resolve_org(user_id)
    predicate, pred_params = org_scope.org_read_predicate(user_id, org_id)
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        set_rls_context(cur, conn, user_id, log_prefix=f"[channels:{provider}:connected]")
        # Active = described member channels: a channel being active IS
        # the permission to post teammate messages here (the structured
        # incident card is separate — it goes only to the single
        # configured incidents channel). Membership is the source of
        # truth, so there's no separate "dismissed" flag to filter on.
        cur.execute(
            f"""SELECT DISTINCT ON (channel_id)
                      channel_id, channel_name, channel_type,
                      detected_platform, metadata_summary, is_member
                 FROM slack_channels
                WHERE provider = %s
                  AND is_member
                  AND metadata_status = 'ready'
                  AND {predicate}
                ORDER BY channel_id, updated_at DESC""",
            (provider, *pred_params),
        )
        rows = cur.fetchall()

    return [
        {
            "channel_id": r[0],
            "channel_name": r[1],
            "channel_type": r[2],
            "detected_platform": r[3],
            "description": r[4] or "(no description)",
            "is_member": r[5],
        }
        for r in rows
    ]


def channel_membership(user_id: str, provider: str, channel_id: str) -> Optional[bool]:
    """True if Aurora is a member of ``channel_id`` (an org ``slack_channels``
    row with ``is_member``), False if not, None if the lookup itself failed.

    Membership is the permission to post. The description (``metadata_status``)
    is only a hint for *choosing* a channel and is deliberately not required
    here — a member channel whose description hasn't generated yet is still a
    legitimate destination (e.g. a thread reply, or a routing-map entry). On a
    DB error returns None so the caller can fail closed without claiming the
    channel is inactive: dropping one teammate message is better than posting
    into a channel the user asked Aurora to stay out of, and a transient error
    must not look like a stale mapping. An unregistered ``provider`` is a
    programming error and raises ``ValueError`` instead.
    """
    _check_provider(provider)
    try:
        org_id = org_scope.resolve_org(user_id)
        predicate, pred_params = org_scope.org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix=f"[channels:{provider}:active]")
            cur.execute(
                f"""SELECT 1 FROM slack_channels
                     WHERE provider = %s AND channel_id = %s AND {predicate}
                       AND is_member
                     LIMIT 1""",
                (provider, channel_id, *pred_params),
            )
            return cur.fetchone() is not None
    except Exception:
        logger.exception("[channels:%s] Could not verify membership of channel %s; refusing post",
                         provider, sanitize(channel_id))
        return None


def get_channel_label(user_id: str, provider: str, channel_id: str) -> str:
    """Return a human 'name (id)' label for a channel so the agent knows which
    channel a message came from — critical for scoped directives like "in this
    channel". Prefers our registered slack_channels row (no API call), falls
    back to the bare id. Never raises for a registered ``provider`` (an
    unregistered one is a programming error and raises ``ValueError``)."""
    _check_provider(provider)
    if not channel_id:
        return "unknown"
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, log_prefix=f"[channels:{provider}:label]")
            cursor.execute(
                """SELECT channel_name FROM slack_channels
                   WHERE user_id = %s AND channel_id = %s AND provider = %s
                   LIMIT 1""",
                (user_id, channel_id, provider),
            )
            row = cursor.fetchone()
        # Registered channel — use its name so it matches routing/descriptions.
        if row and row[0]:
            fmt = _LABEL_FORMATS.get(provider, _DEFAULT_LABEL_FORMAT)
            return fmt.format(name=row[0], id=channel_id)
    except Exception:
        logger.warning("[channels:%s] Could not resolve channel name for %s",
                       provider, sanitize(channel_id), exc_info=True)
    # Unregistered or lookup failed — the id alone still anchors "this channel".
    return channel_id
