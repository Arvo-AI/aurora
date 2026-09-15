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
from datetime import datetime, timezone
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

# chat.update rejections that mean "this message cannot be edited" (gone, not
# ours, too old), where posting instead is the right move. Anything else
# (not_in_channel, invalid_auth, msg_too_long, invalid_blocks, ...) would fail
# the same way as a post.
_UPDATE_ONLY_ERRORS = frozenset({
    "message_not_found",
    "cant_update_message",
    "edit_window_closed",
})

# The anchor card carries one marked context block summarising its recurrences;
# it is replaced (not appended) on every fold. Only cards made of these block
# types are edited — a plain-text message comes back from Slack as rich_text,
# which is not ours to rebuild, and image blocks come back with read-only
# fields (image_width, image_bytes, ...) that chat.update rejects.
RECURRENCE_FOOTER_BLOCK_ID = "aurora_recurrence_footer"
_EDITABLE_BLOCK_TYPES = frozenset({"header", "section", "divider", "context", "actions"})

# Caps for webhook-sourced fields that share one Block Kit section (3000
# chars): past that Slack rejects the whole post as invalid_blocks.
_ALERT_TITLE_MAX = 1500
_SERVICE_MAX = 300

# lookup_message: Slack could not be asked (transport error, rate limit, ...).
LOOKUP_FAILED = object()
_SLACK_MAX_BLOCKS = 50

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


def escaped_fields(incident_data: Dict[str, Any]) -> Tuple[str, str, str]:
    """(alert_title, severity, service) escaped for mrkdwn and capped — the one
    place every card and reply gets these fields, with one default each."""
    alert_title = escape_mrkdwn(truncate(incident_data.get('alert_title') or 'Unknown Alert', _ALERT_TITLE_MAX, "…"))
    severity = escape_mrkdwn((incident_data.get('severity') or 'unknown').title())
    service = escape_mrkdwn(truncate(incident_data.get('service') or 'unknown', _SERVICE_MAX, "…"))
    return alert_title, severity, service


def occurrence_label(incident_data: Dict[str, Any]) -> str:
    return f"occurrence {incident_data.get('occurrence_number') or 1} of {incident_data.get('group_size') or 1}"


def error_block(error_text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": f":x: *Error:* {escape_mrkdwn(error_text)}"}}


def placed_thread_ts(result: Dict[str, Any]) -> Optional[str]:
    """Parent ts of a chat.postMessage response, or None for a top-level post."""
    return (result.get('message') or {}).get('thread_ts') or None


def lookup_message(client: SlackClient, *, channel: str, ts: str) -> Any:
    """The message at `ts`, None when Slack confirms it is gone, or
    LOOKUP_FAILED when Slack could not be asked. Callers treat LOOKUP_FAILED as
    "still present" so a stored parent is never dropped on a transient error."""
    try:
        return client.get_message(channel=channel, ts=ts)
    except ValueError as e:
        logger.warning(f"{_LOG_PREFIX} Could not look up message {ts}: {e}")
        return LOOKUP_FAILED


def message_is_gone(client: SlackClient, *, channel: str, ts: str) -> bool:
    """True only when Slack confirms `ts` is no longer in `channel`."""
    return lookup_message(client, channel=channel, ts=ts) is None


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


def try_update_in_place(client: SlackClient, *, channel: str, ts: str, text: str,
                        blocks: Optional[List[Dict]]) -> Optional[Dict[str, Any]]:
    """chat.update the incident's own "Investigation Started" message into its
    final Complete/Failed card, so the one top-level message always shows the
    outcome and the thread (and its ts, the @mention key) is preserved for
    recurrences. blocks=None means plain text: the old blocks are cleared.

    Returns None when Slack says the message cannot be edited
    (_UPDATE_ONLY_ERRORS) so the caller can post the card instead. Any other
    rejection propagates, and so does a transport failure — the edit may
    already have landed, and posting as well would show the outcome twice.
    """
    try:
        return client.update_message(channel=channel, ts=ts, text=text, blocks=blocks if blocks is not None else [])
    except SlackAPIError as e:
        if e.error not in _UPDATE_ONLY_ERRORS:
            raise
        logger.warning(f"{_LOG_PREFIX} Could not update Started message {ts} in place ({e.error}); posting instead")
        return None


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
    alert_title, severity, service = escaped_fields(incident_data)
    label = occurrence_label(incident_data)
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
        blocks.append(error_block(error_text))
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


def _slack_date(value: Any) -> str:
    """Viewer-local timestamp via Slack's date token, with a UTC fallback."""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    epoch = int(value.timestamp())
    return f"<!date^{epoch}^{{date_short_pretty}} at {{time}}|{value.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC>"


def build_recurrence_footer(incident_data: Dict[str, Any]) -> Dict[str, Any]:
    """Context block for the anchor card: how many times this root cause has
    fired and when the group last did (group_last_fired_at from the
    dispatcher — not this occurrence's own time, which regresses when
    siblings complete out of order). Marked so the next fold replaces it."""
    size = incident_data.get('group_size') or 1
    when = _slack_date(incident_data.get('group_last_fired_at') or incident_data.get('started_at'))
    text = f":repeat: *Still firing* — {size} occurrence{'s' if size != 1 else ''}"
    if when:
        text += f", last {when}"
    text += " · each occurrence is a reply in this thread"
    return {
        "type": "context",
        "block_id": RECURRENCE_FOOTER_BLOCK_ID,
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def update_anchor_recurrence_footer(client: SlackClient, *, channel: str, anchor_ts: str,
                                    incident_data: Dict[str, Any],
                                    message: Optional[Dict[str, Any]] = None) -> bool:
    """After a recurrence reply landed under the anchor, mark the anchor card
    itself so the channel view shows the group is still firing (the replies
    alone are collapsed). Reads the card (or takes `message`, when the caller
    has just fetched it), swaps in the footer, writes it back. Best effort:
    never raises, and leaves the card alone when it is not a Block Kit card
    we built."""
    try:
        if message is None:
            message = client.get_message(channel=channel, ts=anchor_ts)
        if not message:
            return False
        blocks = message.get('blocks') or []
        if not blocks or any(b.get('type') not in _EDITABLE_BLOCK_TYPES for b in blocks):
            logger.info(f"{_LOG_PREFIX} Anchor card {anchor_ts} is not an editable Block Kit card; no recurrence footer")
            return False
        blocks = [b for b in blocks if b.get('block_id') != RECURRENCE_FOOTER_BLOCK_ID]
        blocks.append(build_recurrence_footer(incident_data))
        if len(blocks) > _SLACK_MAX_BLOCKS:
            return False
        client.update_message(channel=channel, ts=anchor_ts, text=message.get('text') or "", blocks=blocks)
        logger.info(f"{_LOG_PREFIX} Updated recurrence footer on anchor card {anchor_ts}")
        return True
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} Could not update recurrence footer on anchor card {anchor_ts}: {e}")
        return False


def build_folded_stub(incident_data: Dict[str, Any], *, incident_url: str, anchor_url: str,
                      error_text: Optional[str] = None) -> Tuple[str, List[Dict]]:
    """Replacement for a folded child's own Started card when that card is
    kept (its thread has replies): says where the outcome went instead of
    reading "In Progress" forever. Returns (fallback_text, blocks)."""
    alert_title, severity, service = escaped_fields(incident_data)
    label = occurrence_label(incident_data)
    anchor_title = _anchor_title(incident_data)
    blocks: List[Dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":repeat: *Folded into <{anchor_url}|{anchor_title}>* — {label}\n"
                    f"*Alert:* {alert_title}\n*Severity:* {severity}\n*Service:* {service}\n"
                    "_The analysis for this occurrence is in that incident's thread._"
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
        blocks.append(error_block(error_text))
    text = f"Folded into {anchor_title} — {label}: {alert_title}"
    return text, blocks


def build_fold_pointer(incident_data: Dict[str, Any], *, anchor_url: str) -> str:
    """One-line reply for a folded child's own Started thread when that
    thread is kept (it has replies): tells the people talking there where
    the outcome went."""
    return (
        f":repeat: Folded into <{anchor_url}|{_anchor_title(incident_data)}> — "
        "the analysis for this occurrence is in that incident's thread."
    )


def _write_slack_message_ts(user_id: str, incident_id: str, sql: str, params: tuple, *, what: str) -> bool:
    """One guarded UPDATE of incidents.slack_message_ts under RLS. True when a
    row changed; False when the guard did not match or the write failed.
    Never raises."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=f"[SlackNotification:{what}]")
                cursor.execute(sql, params)
                changed = cursor.rowcount > 0
                conn.commit()
                return changed
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} Failed to {what} slack_message_ts for incident {incident_id}: {e}")
        return False


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
    return _write_slack_message_ts(
        user_id, incident_id,
        "UPDATE incidents SET slack_message_ts = %s WHERE id = %s AND slack_message_ts IS NOT DISTINCT FROM %s",
        (message_ts, incident_id, replacing), what="backfill",
    )


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
