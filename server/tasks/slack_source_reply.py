"""Deliver the completed RCA beneath its source post, including late posts and retries."""
import json
import logging
import time
from uuid import UUID, uuid5

from requests import HTTPError

from celery_config import celery_app
from connectors.slack_connector.client import SlackAPIError, SlackClient
from utils.auth.stateless_auth import get_credentials_from_db, get_org_preference, set_rls_context
from utils.cache.redis_client import get_redis_client
from utils.db.connection_pool import db_pool
from utils.notifications.slack_alert_match import find_source_thread, source_thread_window_end
from utils.notifications.slack_threading import placed_thread_ts

logger = logging.getLogger(__name__)
MAX_THREAD_LOOKUPS = 10
MAX_DELIVERY_ERRORS = 5
_PERMANENT_SLACK_ERRORS = {
    "account_inactive", "channel_not_found", "invalid_auth", "is_archived",
    "missing_scope", "not_allowed_token_type", "not_authed", "not_in_channel",
    "token_expired", "token_revoked", "no_permission", "restricted_action",
    "cannot_reply_to_message", "restricted_action_thread_locked",
    "restricted_action_non_threadable_channel", "thread_not_found",
    "invalid_arguments", "invalid_blocks", "msg_too_long", "cant_delete_message",
}


def _next_lookup_delay(data, lookups, scan_started):
    window_end = source_thread_window_end(data)
    if window_end is None or scan_started >= window_end or lookups >= MAX_THREAD_LOOKUPS:
        return None
    # A post can arrive after its channel was scanned. Reserve a final scan
    # that starts once the window closes.
    remaining = max(0, window_end - time.time())
    return remaining if lookups == MAX_THREAD_LOOKUPS - 1 else min(30, remaining)


def _next_error_delay(error, errors):
    if errors >= MAX_DELIVERY_ERRORS:
        return None
    if isinstance(error, SlackAPIError) and error.error in _PERMANENT_SLACK_ERRORS:
        return None
    delay = 30 * 2 ** (errors - 1)
    if isinstance(error, HTTPError) and error.response is not None:
        response = error.response
        if response.status_code == 429:
            try:
                return max(delay, int(response.headers["Retry-After"]))
            except (KeyError, TypeError, ValueError):
                return delay
        if 400 <= response.status_code < 500 and response.status_code not in (408, 409, 425):
            return None
    return delay


def queue_source_reply(user_id, incident_id):
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org = set_rls_context(cur, conn, user_id)
        if not org:
            return
        cur.execute("""UPDATE incidents SET slack_source_reply_due = NOW()
                       WHERE id=%s AND user_id=%s AND org_id=%s
                       AND aurora_status='complete' AND rca_unfinished_reason IS NULL
                       AND slack_source_reply_attempts=0 AND slack_source_reply_ts IS NULL AND slack_source_reply_due IS NULL""",
                    (incident_id, user_id, org))
        queued = cur.rowcount
        conn.commit()
    if queued:
        deliver_source_reply.delay(user_id, incident_id)


@celery_app.task(name="tasks.slack_source_reply.retry_due")
def retry_due_source_replies():
    from services.detection.runner import _for_each_org

    def collect(cur, conn, _user, org_id):
        cur.execute("SELECT user_id, id FROM incidents WHERE org_id=%s AND slack_source_reply_due <= NOW()",
                    (org_id,))
        for owner, incident in cur.fetchall():
            deliver_source_reply.delay(owner, str(incident))

    _for_each_org(collect, "retrying due Slack source replies")


def _find_target(cur, client, org, team, data, target):
    incident_id = data["incident_id"]
    # users.conversations is scoped to the connected bot. A newly invited channel
    # can receive an alert before our channel registry has finished syncing.
    channels = [ch["id"] for ch in client.list_bot_channels() if not ch.get("is_archived")]
    if target and target["channel"] not in channels:
        target = None
    if not target and channels:
        # A single Slack post is also ambiguous when two incoming alerts
        # can both claim it (e.g. Sentry refires the same issue).
        cur.execute("""SELECT alert_title, alert_metadata FROM incidents
                       WHERE org_id=%s AND source_type=%s AND id<>%s
                       AND created_at BETWEEN %s - interval '6 minutes' AND %s + interval '6 minutes'
                       AND (alert_metadata->>'correlation_key') IS NOT DISTINCT FROM %s""",
                    (org, data["source_type"], incident_id, data["created_at"], data["created_at"],
                     data["alert_metadata"].get("correlation_key")))
        competing = [dict(source_type=data["source_type"], alert_title=title, alert_metadata=meta)
                     for title, meta in cur.fetchall()]
        match = find_source_thread(client, channels, team, data, competing=competing,
                                   cache=get_redis_client(), org=org)
        if match:
            target = dict(team=team, channel=match[0], ts=match[1])
    return target


def _remove_unthreaded_message(client, target):
    try:
        client.delete_message(target["channel"], target["unthreaded_ts"])
    except SlackAPIError as error:
        if error.error != "message_not_found":
            raise
    del target["unthreaded_ts"]


@celery_app.task(name="tasks.slack_source_reply.deliver")
def deliver_source_reply(user_id, incident_id):
    from utils.notifications.dispatcher import _get_incident_data
    from utils.notifications.slack_notification_service import send_source_rca_reply

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org = set_rls_context(cur, conn, user_id)
        if not org:
            return
        # Serialize immediate, sweep and redelivered tasks without locking an incident
        # row that the standard notification service updates on its own connection.
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", ("source-reply:" + incident_id,))
        if not cur.fetchone()[0]:
            return
        cur.execute("""SELECT slack_source_reply_attempts, slack_source_reply_errors, slack_source_target
                       FROM incidents WHERE id=%s AND org_id=%s AND user_id=%s
                       AND EXISTS (SELECT 1 FROM users u WHERE u.id=incidents.user_id AND u.org_id=incidents.org_id)
                       AND slack_source_reply_due <= NOW() AND slack_source_reply_ts IS NULL""",
                    (incident_id, org, user_id))
        row = cur.fetchone()
        if not row:
            return
        attempts, errors, target = row
        reply_ts = None
        retry_delay = None
        try:
            data = _get_incident_data(incident_id, user_id)
            if not data:
                raise ValueError("Incident unavailable for Slack reply")
            enabled = get_org_preference(org, "slack_investigation_complete_notifications", default=True, strict=True)
            cleanup_pending = target and target.get("unthreaded_ts")
            if cleanup_pending or (enabled and data["aurora_status"] == "complete" and not data.get("unfinished_reason")):
                creds = get_credentials_from_db(user_id, "slack") or {}
                if not creds.get("access_token") or not creds.get("team_id"):
                    raise ValueError("Slack workspace credentials unavailable")
                client = SlackClient(creds["access_token"], retry_rate_limits=False)
                team = creds["team_id"]
                if target and target.get("team") != team:
                    logger.warning("Stopping Slack RCA delivery for %s: workspace changed", incident_id)
                elif cleanup_pending:
                    _remove_unthreaded_message(client, target)
                else:
                    if not data.get("aurora_summary"):
                        raise ValueError("Completed RCA text unavailable")
                    scan_started = time.time()
                    target = _find_target(cur, client, org, team, data, target)
                    if target:
                        delivery_id = str(uuid5(UUID(incident_id), "slack-source-reply"))
                        result = send_source_rca_reply(client, target["channel"], target["ts"], data, delivery_id)
                        if placed_thread_ts(result) == target["ts"]:
                            reply_ts = result["ts"]
                        else:
                            # Slack can accept a reply as a channel post if the parent
                            # disappears. Retain the ts until cleanup succeeds.
                            target["unthreaded_ts"] = result["ts"]
                            _remove_unthreaded_message(client, target)
                    else:
                        lookups = attempts - errors + 1
                        retry_delay = _next_lookup_delay(data, lookups, scan_started)
                        if retry_delay is None:
                            logger.info("No matching Slack thread for %s after %s lookups", incident_id, lookups)
        except Exception as error:
            errors += 1
            retry_delay = _next_error_delay(error, errors)
            logger.warning("Slack RCA delivery failed for %s (error %s, next delay %s)", incident_id,
                           errors, retry_delay, exc_info=True)
        cur.execute("""UPDATE incidents SET slack_source_reply_ts=%s, slack_source_target=%s,
                       slack_source_reply_attempts=slack_source_reply_attempts+1, slack_source_reply_errors=%s,
                       slack_source_reply_due=clock_timestamp() + %s * interval '1 second'
                       WHERE id=%s AND org_id=%s""",
                    (reply_ts, json.dumps(target) if target else None, errors, retry_delay, incident_id, org))
        conn.commit()
