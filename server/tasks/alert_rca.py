"""Publish RCA work recorded in the same transaction as its source incident."""
import logging
from uuid import UUID, uuid5

from celery_config import celery_app
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


def dispatch_pending_rca(user_id, incident_id):
    from chat.background.rca_prompt_builder import build_rca_prompt
    from chat.background.task import create_background_chat_session, run_background_chat

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix="[AlertRCA]")
        if not org_id:
            return
        # NO KEY UPDATE is compatible with the session's incident foreign key.
        cur.execute("""
            SELECT source_type, source_alert_id, alert_title, alert_metadata
              FROM incidents WHERE id=%s AND user_id=%s AND org_id=%s AND alert_rca_pending
              FOR NO KEY UPDATE SKIP LOCKED
        """, (incident_id, user_id, org_id))
        row = cur.fetchone()
        if not row:
            return
        source, source_id, title, meta = row
        session_id = str(uuid5(UUID(str(incident_id)), "alert-rca"))
        # The original publish may have succeeded even when its receipt was
        # lost. A worker that has linked the session already owns this RCA.
        cur.execute("SELECT 1 FROM incidents WHERE id=%s AND aurora_chat_session_id=%s", (incident_id, session_id))
        if cur.fetchone():
            cur.execute("UPDATE incidents SET alert_rca_pending=FALSE WHERE id=%s", (incident_id,))
            conn.commit()
            return
        table = {"cloudwatch": "cloudwatch_alarms", "sentry": "sentry_events"}[source]
        cur.execute(f"SELECT payload FROM {table} WHERE id=%s AND org_id=%s AND user_id=%s",
                    (source_id, org_id, user_id))
        payload = cur.fetchone()[0]
        if source == "cloudwatch" and payload.get("Type") == "Notification":
            payload = {key: payload.get(key) for key in ("Subject", "Message")}
        prompt, rail = build_rca_prompt(source, title, payload, user_id=user_id)
        trigger = {"source": source, "alert_delivery": True}
        if source == "cloudwatch":
            trigger.update(alarm_name=title, state="ALARM")
        else:
            trigger.update({key: meta.get(key) for key in ("issueId", "resource", "action")})
        create_background_chat_session(user_id, f"RCA: {title}", trigger_metadata=trigger,
                                       incident_id=str(incident_id), org_id=org_id, session_id=session_id)
        run_background_chat.apply_async(task_id=session_id, kwargs=dict(
            user_id=user_id, session_id=session_id, incident_id=str(incident_id),
            initial_message=prompt, trigger_metadata=trigger, rail_text=rail))
        cur.execute("UPDATE incidents SET alert_rca_pending=FALSE, rca_celery_task_id=%s WHERE id=%s",
                    (session_id, incident_id))
        conn.commit()


@celery_app.task(name="tasks.alert_rca.retry_pending")
def retry_pending():
    # Revisit durable requests left behind by an unavailable broker or worker.
    from services.detection.runner import _for_each_org

    def collect(cur, conn, _user, org_id):
        cur.execute("SELECT user_id, id FROM incidents WHERE org_id=%s AND alert_rca_pending "
                    "ORDER BY created_at", (org_id,))
        pending = cur.fetchall()
        conn.commit()
        from tasks.slack_source_reply import deliver_source_reply
        cur.execute("SELECT user_id, id FROM incidents WHERE org_id=%s AND slack_source_reply_due <= NOW()",
                    (org_id,))
        for owner, incident in cur.fetchall():
            deliver_source_reply.delay(owner, str(incident))
        for user_id, incident_id in pending:
            try:
                dispatch_pending_rca(user_id, str(incident_id))
            except Exception:
                logger.exception("RCA dispatch pending for incident %s", incident_id)

    _for_each_org(collect, "publishing pending alert RCAs")
