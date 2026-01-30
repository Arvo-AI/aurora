"""
Celery tasks for scheduled infrastructure discovery.
"""

import json
import logging

from celery_config import celery_app

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ('gcp', 'aws', 'azure', 'ovh', 'scaleway', 'tailscale')


def _parse_credentials(raw_credentials):
    """Parse credentials from a DB row value into a dict.

    Handles None, empty strings, JSON strings, and dicts.
    """
    if not raw_credentials:
        return {}
    if isinstance(raw_credentials, str):
        try:
            return json.loads(raw_credentials)
        except (json.JSONDecodeError, ValueError):
            return {}
    return raw_credentials


@celery_app.task(name="services.discovery.tasks.run_full_discovery", bind=True, max_retries=0)
def run_full_discovery(self):
    """Run full infrastructure discovery for all users with connected cloud providers.

    Scheduled by Celery beat to run every hour.
    Can also be triggered on-demand via POST /api/graph/discover.
    """
    from utils.db.db_utils import connect_to_db_as_admin
    from services.discovery.discovery_service import run_discovery_for_user

    logger.info("[Discovery Task] Starting full discovery run")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()

        cur.execute("""
            SELECT DISTINCT user_id, provider, credentials
            FROM user_connections
            WHERE status = 'connected'
            AND provider IN %s
            ORDER BY user_id
        """, (SUPPORTED_PROVIDERS,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            logger.info("[Discovery Task] No users with connected cloud providers")
            return {"status": "no_users", "users_processed": 0}

        # Group by user
        users = {}
        for user_id, provider, raw_credentials in rows:
            users.setdefault(user_id, {})[provider] = _parse_credentials(raw_credentials)

        logger.info(f"[Discovery Task] Processing {len(users)} users")

        results = []
        for user_id, providers in users.items():
            try:
                summary = run_discovery_for_user(user_id, providers)
                results.append(summary)
                logger.info(f"[Discovery Task] User {user_id}: {summary.get('phase1_nodes', 0)} nodes discovered")
            except Exception as e:
                logger.error(f"[Discovery Task] Failed for user {user_id}: {e}")
                results.append({"user_id": user_id, "error": str(e)})

        return {
            "status": "completed",
            "users_processed": len(users),
            "results": results,
        }

    except Exception as e:
        logger.error(f"[Discovery Task] Fatal error: {e}")
        return {"status": "error", "error": str(e)}


@celery_app.task(name="services.discovery.tasks.run_user_discovery", bind=True, max_retries=0)
def run_user_discovery(self, user_id):
    """Run discovery for a single user. Called on-demand via API."""
    from utils.db.db_utils import connect_to_db_as_admin
    from services.discovery.discovery_service import run_discovery_for_user

    logger.info(f"[Discovery Task] Starting on-demand discovery for user {user_id}")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()
        cur.execute("""
            SELECT provider, credentials
            FROM user_connections
            WHERE user_id = %s AND status = 'connected'
            AND provider IN %s
        """, (user_id, SUPPORTED_PROVIDERS))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return {"status": "no_providers", "user_id": user_id}

        providers = {provider: _parse_credentials(raw_creds) for provider, raw_creds in rows}

        summary = run_discovery_for_user(user_id, providers)
        return summary

    except Exception as e:
        logger.error(f"[Discovery Task] Failed for user {user_id}: {e}")
        return {"status": "error", "user_id": user_id, "error": str(e)}


@celery_app.task(name="services.discovery.tasks.mark_stale_services", bind=True, max_retries=0)
def mark_stale_services(self):
    """Mark services not updated in 7 days as stale. Runs daily at 3 AM."""
    from utils.db.db_utils import connect_to_db_as_admin
    from services.graph.memgraph_client import get_memgraph_client

    logger.info("[Discovery Task] Starting stale service detection")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM user_connections WHERE status = 'connected'")
        user_ids = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()

        client = get_memgraph_client()
        total_marked = 0
        for user_id in user_ids:
            try:
                marked = client.mark_stale_services(user_id, stale_days=7)
                total_marked += marked
                if marked > 0:
                    logger.info(f"[Discovery Task] Marked {marked} stale services for user {user_id}")
            except Exception as e:
                logger.error(f"[Discovery Task] Stale detection failed for user {user_id}: {e}")

        logger.info(f"[Discovery Task] Stale detection complete: {total_marked} services marked")
        return {"status": "completed", "total_marked": total_marked}

    except Exception as e:
        logger.error(f"[Discovery Task] Stale detection fatal error: {e}")
        return {"status": "error", "error": str(e)}
