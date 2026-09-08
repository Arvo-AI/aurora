"""
Slack thread consolidation for incident notifications (root-cause dedup layer 3).

One channel message per root cause: an incident's "Analysis Complete" /
"Investigation Failed" post replies in a thread instead of landing top-level.
Standalone incidents reply under their own "Investigation Started" message;
folded recurrences (incidents.recurrence_of_incident_id set) reply under the
anchor's message with a compact "Still firing" card and retire their own
Started message. Every failure path degrades to today's top-level post.

incidents.slack_message_ts is the incident's thread parent and the key that
routes Slack @mentions back to the incident. It is only ever written here by
compare-and-set, and a stored parent is only replaced or cleared once Slack
has confirmed it is gone.

The group fields (recurrence_of, anchor_slack_message_ts, anchor_alert_title,
occurrence_number, group_size) come from dispatcher._get_incident_data.
"""

import html
import logging
from typing import Any, Dict, List, Optional, Tuple

from connectors.slack_connector.client import SlackAPIError, SlackClient
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.text.text_utils import truncate

logger = logging.getLogger(__name__)

_ANCHOR_TITLE_MAX = 120
_LOG_PREFIX = "[SlackNotification:thread]"

# chat.postMessage rejections that are about the thread itself, where a
# top-level retry can succeed. Anything else (not_in_channel, invalid_auth,
# msg_too_long, invalid_blocks, ...) fails identically top-level.
_THREAD_ONLY_ERRORS = frozenset({
    "cannot_reply_to_message",
    "restricted_action_thread_locked",
    "restricted_action_non_threadable_channel",
})

# clear_child_started_message outcomes
RELEASED = "released"          # stored ts cleared (message deleted or already gone)
KEPT_REPLIES = "kept_replies"  # message has replies: kept, still the thread key
KEPT = "kept"                  # nothing changed (no ts, lookup/db failure, fold no longer holds)


def is_folded_child(incident_data: Dict[str, Any]) -> bool:
    """True when this incident has been folded into an anchor (layer 1)."""
    return bool(incident_data.get('recurrence_of'))


def escape_mrkdwn(text: Any) -> str:
    """Escape a value for Slack mrkdwn (only &, < and > per Slack's formatting
    rules) so titles from monitoring webhooks cannot inject links, mentions
    or <!channel>."""
    return html.escape(str(text), quote=False)


def placed_thread_ts(result: Dict[str, Any]) -> Optional[str]:
    """Parent ts of a chat.postMessage response, or None for a top-level post."""
    return (result.get('message') or {}).get('thread_ts') or None


def message_is_gone(client: SlackClient, *, channel: str, ts: str) -> bool:
    """True only when Slack confirms `ts` is no longer in `channel`. A failed
    lookup counts as present so a stored parent is never dropped on a
    transient error."""
    try:
        return client.get_message(channel=channel, ts=ts) is None
    except ValueError as e:
        logger.warning(f"{_LOG_PREFIX} Could not look up message {ts}: {e}")
        return False


def try_post_threaded(client: SlackClient, *, channel: str, text: str,
                      blocks: Optional[List[Dict]], thread_ts: str,
                      require_threaded: bool = False) -> Optional[Dict[str, Any]]:
    """chat.postMessage with thread_ts. Returns None when Slack rejected the
    post for a thread-specific reason (_THREAD_ONLY_ERRORS) so the caller can
    post top-level. Any other rejection propagates — it would fail the same
    way top-level — and so does a transport failure (timeout, connection
    error, exhausted rate-limit retries): the message may already have
    landed, and a second attempt would duplicate it.

    Slack does not error on a missing or deleted parent: it silently posts
    the message top-level and omits message.thread_ts from the response
    (verified live). With require_threaded=True that outcome is treated as a
    failure — the stray top-level post is removed (best effort) and None is
    returned. If the stray cannot be removed the response is returned as-is;
    callers that need a real reply must check placed_thread_ts(result).
    """
    try:
        result = client.send_message(channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)
    except SlackAPIError as e:
        if e.error not in _THREAD_ONLY_ERRORS:
            raise
        logger.warning(f"{_LOG_PREFIX} Threaded post under {thread_ts} rejected ({e.error}); degrading to top-level")
        return None
    if require_threaded and placed_thread_ts(result) != thread_ts:
        stray_ts = result.get('ts')
        logger.warning(
            f"{_LOG_PREFIX} Slack placed reply top-level (parent {thread_ts} missing); "
            f"removing stray post {stray_ts} and degrading to top-level"
        )
        if stray_ts:
            try:
                client.delete_message(channel=channel, ts=stray_ts)
            except Exception as e:
                # The stray is a visible notification already; keep it rather
                # than have the caller post a second card for the same incident.
                logger.warning(f"{_LOG_PREFIX} Failed to remove stray post {stray_ts}; keeping it: {e}")
                return result
        return None
    return result


def post_with_thread_fallback(client: SlackClient, *, channel: str, text: str,
                              blocks: Optional[List[Dict]] = None,
                              thread_ts: Optional[str] = None) -> Dict[str, Any]:
    """Post in the thread when thread_ts is set and Slack accepts it; otherwise
    today's top-level post. A top-level failure propagates to the caller.
    The response's message.thread_ts tells where the post actually landed."""
    if thread_ts:
        result = try_post_threaded(client, channel=channel, text=text, blocks=blocks, thread_ts=thread_ts)
        if result:
            return result
    return client.send_message(channel=channel, text=text, blocks=blocks)


def _anchor_title(incident_data: Dict[str, Any]) -> str:
    return escape_mrkdwn(truncate(
        (incident_data.get('anchor_alert_title') or 'original incident').strip(), _ANCHOR_TITLE_MAX, "…",
    ))


def build_recurrence_reply(incident_data: Dict[str, Any], *, incident_url: str, anchor_url: str,
                           error_text: Optional[str] = None) -> Tuple[str, List[Dict]]:
    """Compact thread reply for a folded child. No RCA paragraph or
    suggestions: the anchor's thread already carries them. error_text turns
    it into the failed variant.

    Returns (fallback_text, blocks).
    """
    alert_title = escape_mrkdwn(incident_data.get('alert_title') or 'Unknown Alert')
    severity = escape_mrkdwn((incident_data.get('severity') or 'unknown').title())
    service = escape_mrkdwn(incident_data.get('service') or 'unknown')
    label = f"occurrence {incident_data.get('occurrence_number') or 1} of {incident_data.get('group_size') or 1}"
    anchor_title = _anchor_title(incident_data)

    blocks: List[Dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":repeat: *Still firing — {label}*\n"
                    f"*Alert:* {alert_title}\n*Severity:* {severity}\n*Service:* {service}"
                ),
            },
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "View Occurrence"},
                "url": incident_url,
            },
        },
    ]
    if error_text:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":x: *Error:* {escape_mrkdwn(error_text)}"},
        })
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"Recurrence of <{anchor_url}|{anchor_title}>"},
        ],
    })

    text = f"Still firing — {label}: {alert_title} ({incident_url})"
    if error_text:
        text = "Investigation failed — " + text
    return text, blocks


def build_fold_pointer(incident_data: Dict[str, Any], *, anchor_url: str) -> str:
    """One-line reply for a folded child's own Started thread when that
    thread is kept (it has replies): tells the people talking there where
    the outcome went."""
    return (
        f":repeat: Folded into <{anchor_url}|{_anchor_title(incident_data)}> — "
        "the analysis for this occurrence is in that incident's thread."
    )


def _write_slack_message_ts(user_id: str, incident_id: str, sql: str, params: tuple, *, what: str) -> int:
    """One guarded UPDATE of incidents.slack_message_ts under RLS. Returns the
    number of rows changed (0 when the guard did not match or the write
    failed). Never raises."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=f"[SlackNotification:{what}]")
                cursor.execute(sql, params)
                changed = int(cursor.rowcount or 0)
                conn.commit()
                return max(changed, 0)
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} Failed to {what} slack_message_ts for incident {incident_id}: {e}")
        return 0


def clear_child_started_message(client: SlackClient, *, channel: str, user_id: str,
                                incident_data: Dict[str, Any]) -> str:
    """Retire a folded child's own "Investigation Started" message once its
    completion has landed in the anchor's thread: release the stored ts,
    then delete the message. The ts is released first, and only while the
    fold still holds — layer 1's mutual-fold tie-break can unfold an incident
    concurrently — so a message is never deleted from under an incident that
    has just become an anchor again. Later posts for this incident go
    top-level (unfold) rather than under a deleted parent.

    The message is kept — and stays the incident's thread key — when it
    already has replies: deleting it would orphan that conversation under a
    tombstone and stop @mentions there from resolving to the incident. The
    caller posts a pointer into that thread instead (KEPT_REPLIES).
    Never raises. Returns RELEASED, KEPT_REPLIES or KEPT."""
    ts = incident_data.get('slack_message_ts')
    if not ts:
        return KEPT
    incident_id = incident_data.get('incident_id')
    try:
        message = client.get_message(channel=channel, ts=ts)
    except ValueError as e:
        logger.warning(f"{_LOG_PREFIX} Could not look up started message {ts} for incident {incident_id}: {e}")
        return KEPT
    if message is not None and message.get('reply_count'):
        logger.info(f"{_LOG_PREFIX} Keeping started message {ts} for folded incident {incident_id}: it has replies")
        return KEPT_REPLIES
    released = _write_slack_message_ts(
        user_id, incident_id,
        "UPDATE incidents SET slack_message_ts = NULL "
        "WHERE id = %s AND slack_message_ts = %s AND recurrence_of_incident_id = %s",
        (incident_id, ts, incident_data.get('recurrence_of')), what="clear",
    )
    if not released:
        logger.info(
            f"{_LOG_PREFIX} Keeping started message {ts} for incident {incident_id}: "
            "it is no longer a folded child or its thread key changed"
        )
        return KEPT
    if message is None:
        logger.info(f"{_LOG_PREFIX} Started message {ts} for folded incident {incident_id} is already gone")
        return RELEASED
    try:
        client.delete_message(channel=channel, ts=ts)
    except Exception as e:
        logger.warning(
            f"{_LOG_PREFIX} Failed to delete started message {ts} for incident {incident_id} "
            f"(its thread key is already released): {e}"
        )
        return RELEASED
    logger.info(f"{_LOG_PREFIX} Deleted started message {ts} for folded incident {incident_id}")
    return RELEASED


def clear_anchor_message_ts(user_id: str, anchor_id: str, ts: str) -> None:
    """Forget an anchor's thread parent once Slack has confirmed it is gone, so
    later recurrences stop posting under it (and the next top-level card can
    seed a new one). Compare-and-set on the value that was read."""
    _write_slack_message_ts(
        user_id, anchor_id,
        "UPDATE incidents SET slack_message_ts = NULL WHERE id = %s AND slack_message_ts = %s",
        (anchor_id, ts), what="clear-anchor",
    )


def backfill_thread_parent_ts(user_id: str, incident_id: str, message_ts: str, *,
                              replacing: Optional[str] = None) -> bool:
    """Record a top-level post as the incident's thread parent so later
    recurrences can thread under it. Compare-and-set on the value read at
    notification time (`replacing`; None means "only when unset") so a ts
    written concurrently is never overwritten. Never raises. True when the
    row changed."""
    if not message_ts:
        return False
    return bool(_write_slack_message_ts(
        user_id, incident_id,
        "UPDATE incidents SET slack_message_ts = %s WHERE id = %s AND slack_message_ts IS NOT DISTINCT FROM %s",
        (message_ts, incident_id, replacing), what="backfill",
    ))


def record_thread_parent(client: SlackClient, *, channel: str, user_id: str, incident_id: str,
                         result: Dict[str, Any], stored: Optional[str] = None) -> bool:
    """After a Started/Complete/Failed post landed top-level, make it the
    incident's thread parent. A stored parent is replaced only once Slack
    confirms it is gone — a top-level landing alone is not proof (Slack also
    posts top-level on a thread-specific rejection, or when the stored ts
    lives in another channel) — so a live Started thread never loses its
    @mention key. Never raises. True when the ts was recorded."""
    ts = result.get('ts')
    if placed_thread_ts(result) or not ts:
        return False
    if stored and not message_is_gone(client, channel=channel, ts=stored):
        logger.info(
            f"{_LOG_PREFIX} Post {ts} for incident {incident_id} landed top-level but parent {stored} "
            "is still in Slack; keeping it as the thread parent"
        )
        return False
    return backfill_thread_parent_ts(user_id, incident_id, ts, replacing=stored)
