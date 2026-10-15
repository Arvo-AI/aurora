"""Hourly repair for connector rows whose owner is already gone.

Member removal reassigns immediately. This only covers a delete that skipped
that path. Hourly is enough; the action scheduler runs every minute.
"""
import logging

from celery_config import celery_app
from services.connectors.ownership import reassign_connectors_to_admin
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


@celery_app.task(name="services.connectors.heal.reassign_orphaned_connectors")
def reassign_orphaned_connectors():
    """Move leftover org credentials to a living admin, once an hour."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ON (org_id) id, org_id "
                    "FROM users WHERE org_id IS NOT NULL "
                    "ORDER BY org_id, id"
                )
                for user_id, org_id in cur.fetchall():
                    if not set_rls_context(cur, conn, user_id, log_prefix="[ConnectorHeal]"):
                        continue
                    # A failed UPDATE aborts the transaction for every later org.
                    try:
                        if reassign_connectors_to_admin(cur, org_id):
                            conn.commit()
                    except Exception:
                        logger.exception(
                            "[ConnectorHeal] Reassignment failed for org %s",
                            sanitize(org_id),
                        )
                        conn.rollback()
    except Exception:
        logger.exception("[ConnectorHeal] Failed to reassign orphaned connectors")
