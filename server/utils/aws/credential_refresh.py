"""
Proactive STS credential refresh for multi-account AWS workspaces.

Iterates active AWS connections and re-assumes roles whose cached credentials
are within 10 minutes of expiry, keeping the in-memory cache warm so that
discovery and chat commands don't block on STS calls.
"""

import logging
import time
from celery_config import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="utils.aws.credential_refresh.refresh_aws_credentials")
def refresh_aws_credentials():
    """Proactively refresh STS credentials that are close to expiry.

    Runs as a Celery beat task.  For every active AWS connection across all
    users, checks whether cached credentials expire within the next 10 minutes.
    If so, re-assumes the role to refresh the cache entry.
    """
    from utils.aws.aws_sts_client import _credential_cache, get_sts_client

    current_time = int(time.time())
    refresh_window = 600  # 10 minutes before expiry
    refreshed = 0
    skipped = 0

    entries_to_refresh = []
    for cache_key, creds in list(_credential_cache.items()):
        remaining = creds["expiration"] - current_time
        if 0 < remaining <= refresh_window:
            entries_to_refresh.append(cache_key)

    if not entries_to_refresh:
        logger.debug("No AWS credentials need proactive refresh")
        return {"refreshed": 0, "skipped": 0}

    from utils.db.db_utils import connect_to_db_as_admin

    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT uc.user_id, uc.role_arn, uc.region, w.aws_external_id, w.id "
                "FROM user_connections uc "
                "JOIN workspaces w ON w.user_id = uc.user_id "
                "WHERE uc.provider = 'aws' AND uc.status = 'active' "
                "AND w.aws_external_id IS NOT NULL"
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error("Failed to query active AWS connections for refresh: %s", e)
        return {"refreshed": 0, "error": str(e)}
    finally:
        if conn:
            conn.close()

    for _user_id, role_arn, region, external_id, workspace_id in rows:
        region = region or "us-east-1"
        try:
            client = get_sts_client(region)
            client.assume_workspace_role(
                role_arn=role_arn,
                external_id=external_id,
                workspace_id=workspace_id,
            )
            refreshed += 1
        except Exception as e:
            logger.warning("Proactive refresh failed for role %s: %s", role_arn, e)
            skipped += 1

    logger.info("Proactive AWS credential refresh: %d refreshed, %d skipped", refreshed, skipped)
    return {"refreshed": refreshed, "skipped": skipped}
