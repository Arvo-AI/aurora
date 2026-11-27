"""Deliver the completed RCA beneath its source post, including late posts and retries."""
import json
import logging
from uuid import UUID, uuid5

from celery_config import celery_app
from utils.auth.stateless_auth import get_credentials_from_db, get_org_preference, set_rls_context
from utils.cache.redis_client import get_redis_client
from utils.db.connection_pool import db_pool
from utils.notifications.slack_alert_match import find_source_thread

logger = logging.getLogger(__name__)


def queue_source_reply(user_id, incident_id):
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org = set_rls_context(cur, conn, user_id)
        if not org:
            return
        cur.execute("""UPDATE incidents SET slack_source_reply_due = NOW()
                       WHERE id=%s AND user_id=%s AND org_id=%s
                       AND slack_source_reply_ts IS NULL AND slack_source_reply_due IS NULL""",
                    (incident_id, user_id, org))
        queued = cur.rowcount
        conn.commit()
    if queued:
        deliver_source_reply.delay(user_id, incident_id)


def _find_target(cur, client, org, team, data, target):
    incident_id = data["incident_id"]
    cur.execute("""SELECT DISTINCT channel_id FROM slack_channels WHERE org_id=%s AND team_id=%s
                   AND provider='slack' AND is_member AND NOT is_archived ORDER BY channel_id""",
                (org, team))
    channels = [row[0] for row in cur.fetchall()]
    if target and target["channel"] not in channels:
        target = None
    if not target and channels:
        # A single Slack post is also ambiguous when two incoming alerts
        # can both claim it (e.g. Sentry refires the same issue).
        cur.execute("""SELECT alert_title, alert_metadata FROM incidents
                       WHERE org_id=%s AND source_type=%s AND id<>%s
                       AND created_at BETWEEN %s - interval '6 minutes' AND %s + interval '6 minutes'
                       AND alert_metadata->>'correlation_key' = %s""",
                    (org, data["source_type"], incident_id, data["created_at"], data["created_at"],
                     data["alert_metadata"].get("correlation_key")))
        competing = [dict(source_type=data["source_type"], alert_title=title, alert_metadata=meta)
                     for title, meta in cur.fetchall()]
        match = find_source_thread(client, channels, team, data, competing=competing,
                                   cache=get_redis_client(), org=org)
        if match:
            target = dict(team=team, channel=match[0], ts=match[1])
    return target


@celery_app.task(name="tasks.slack_source_reply.deliver")
def deliver_source_reply(user_id, incident_id):
    from connectors.slack_connector.client import SlackClient
    from utils.notifications.dispatcher import _get_incident_data, _enrich_incident_summary
    from utils.notifications.slack_notification_service import (
        send_source_rca_reply, send_slack_investigation_completed_notification,
    )

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org = set_rls_context(cur, conn, user_id)
        if not org:
            return
        # Serialize immediate, sweep and redelivered tasks without locking an incident
        # row that the standard notification service updates on its own connection.
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", ("source-reply:" + incident_id,))
        if not cur.fetchone()[0]:
            return
        cur.execute("""SELECT slack_source_reply_attempts, aurora_chat_session_id, slack_source_target
                       FROM incidents WHERE id=%s AND org_id=%s AND user_id=%s
                       AND EXISTS (SELECT 1 FROM users u WHERE u.id=incidents.user_id AND u.org_id=incidents.org_id)
                       AND slack_source_reply_due <= NOW() AND slack_source_reply_ts IS NULL""",
                    (incident_id, org, user_id))
        row = cur.fetchone()
        if not row:
            return
        attempt, session, target = row
        done = None
        try:
            data = _get_incident_data(incident_id, user_id)
            creds = get_credentials_from_db(user_id, "slack") or {}
            if not get_org_preference(org, 'slack_investigation_complete_notifications', default=True, strict=True):
                done = "disabled"
            elif not creds.get("access_token"):
                raise ValueError("Slack credentials unavailable")
            elif not data:
                raise ValueError("Incident unavailable for Slack reply")
            else:
                client = SlackClient(creds["access_token"])
                team = creds.get("team_id")
                if target and target.get("team") != team:
                    raise ValueError("Source Slack workspace changed")
                target = _find_target(cur, client, org, team, data, target)
                if session:
                    _enrich_incident_summary(data, str(session), user_id)
                if not data.get("aurora_summary"):
                    raise ValueError("Completed RCA text unavailable")
                if target:
                    result = send_source_rca_reply(client, target["channel"], target["ts"], data,
                                                   str(uuid5(UUID(incident_id), "slack-source-reply")))
                    if result:
                        done = result["ts"]
                    # A definitive thread rejection (deleted or locked parent)
                    # can use the configured channel. A transport failure raises
                    # and keeps this target for the next attempt.
                if not done and (target or attempt >= 3):
                    logger.info("No usable source thread for %s; using its configured incidents channel", incident_id)
                    if send_slack_investigation_completed_notification(user_id, data):
                        done = "fallback"
                    else:
                        raise ValueError("Slack rejected the fallback notification")
        except Exception:
            logger.exception("Slack RCA delivery pending for incident %s", incident_id)
        cur.execute("""UPDATE incidents SET slack_source_reply_ts=%s, slack_source_target=%s,
                       slack_source_reply_attempts=slack_source_reply_attempts+1,
                       slack_source_reply_due=CASE WHEN %s THEN NULL ELSE NOW() + %s * interval '1 second' END
                       WHERE id=%s AND org_id=%s""",
                    (done, json.dumps(target) if target else None, bool(done),
                     min(3600, 30 * 2 ** min(attempt, 7)), incident_id, org))
        conn.commit()
