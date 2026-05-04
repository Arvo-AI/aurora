"""Periodic task to dispatch scheduled actions."""
import logging
from datetime import datetime, timezone

from celery_config import celery_app
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


@celery_app.task(name="services.actions.scheduler.run_scheduled_actions")
def run_scheduled_actions():
    """Check all on_schedule actions and dispatch any that are due."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.id, a.org_id, a.created_by,
                           (a.trigger_config->>'interval_seconds')::int AS interval_seconds,
                           MAX(r.started_at) AS last_run_at
                    FROM actions a
                    LEFT JOIN action_runs r ON r.action_id = a.id
                    WHERE a.trigger_type = 'on_schedule' AND a.enabled = true
                    GROUP BY a.id
                """)
                rows = cur.fetchall()
    except Exception:
        logger.exception("[ActionScheduler] Failed to query scheduled actions")
        return

    now = datetime.now(timezone.utc)
    dispatched = 0

    for action_id, org_id, created_by, interval_seconds, last_run_at in rows:
        if not interval_seconds or interval_seconds < 300:
            continue

        if last_run_at:
            if last_run_at.tzinfo is None:
                last_run_at = last_run_at.replace(tzinfo=timezone.utc)
            elapsed = (now - last_run_at).total_seconds()
            if elapsed < interval_seconds:
                continue

        try:
            from services.actions.executor import dispatch_action
            dispatch_action(
                action_id=str(action_id),
                user_id=created_by,
                trigger_context={"source": "schedule"},
            )
            dispatched += 1
        except Exception:
            logger.exception("[ActionScheduler] Failed to dispatch action %s", action_id)

    if dispatched:
        logger.info("[ActionScheduler] Dispatched %d scheduled action(s)", dispatched)
