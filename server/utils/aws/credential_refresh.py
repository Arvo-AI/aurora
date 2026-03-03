"""
Proactive STS credential refresh for multi-account AWS workspaces.

Iterates active AWS connections and re-assumes roles whose cached credentials
are within the refresh window of expiry, keeping the in-memory cache warm so
that discovery and chat commands don't block on STS calls.
"""

import logging
import time
from celery_config import celery_app

logger = logging.getLogger(__name__)

REFRESH_WINDOW_SECONDS = 900  # 15 minutes — wider than the 10-min beat schedule


@celery_app.task(name="utils.aws.credential_refresh.refresh_aws_credentials")
def refresh_aws_credentials():
    """Proactively refresh STS credentials that are close to expiry.

    Runs as a Celery beat task.  For every active AWS connection across all
    users, checks whether cached credentials expire within the refresh window.
    If so, re-assumes the role to refresh the cache entry.
    """
    from utils.aws.aws_sts_client import _credential_cache, assume_workspace_role

    current_time = int(time.time())
    refreshed = 0
    skipped = 0

    expiring_cache_keys = set()
    for key, creds in _credential_cache.items():
        ttl = creds["expiration"] - current_time
        if 0 < ttl <= REFRESH_WINDOW_SECONDS:
            expiring_cache_keys.add(key)

    if not expiring_cache_keys:
        logger.debug("No AWS credentials need proactive refresh")
        return {"refreshed": 0, "skipped": 0}

    expiring_role_arns = {k.split(":")[0] for k in expiring_cache_keys}

    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT uc.user_id, uc.role_arn, uc.region, "
                    "       uc.workspace_id, w.aws_external_id "
                    "FROM user_connections uc "
                    "JOIN workspaces w ON w.id = uc.workspace_id "
                    "WHERE uc.provider = 'aws' AND uc.status = 'active' "
                    "AND uc.workspace_id IS NOT NULL "
                    "AND w.aws_external_id IS NOT NULL"
                )
                rows = cur.fetchall()
    except Exception as e:
        logger.error("Failed to query active AWS connections for refresh: %s", e)
        return {"refreshed": 0, "error": str(e)}

    for _user_id, role_arn, region, workspace_id, external_id in rows:
        if role_arn not in expiring_role_arns:
            skipped += 1
            continue
        region = region or "us-east-1"
        try:
            assume_workspace_role(
                role_arn=role_arn,
                external_id=external_id,
                workspace_id=workspace_id,
                region=region,
            )
            refreshed += 1
        except Exception as e:
            logger.warning("Proactive refresh failed for role %s: %s", role_arn, e)
            skipped += 1

    logger.info("Proactive AWS credential refresh: %d refreshed, %d skipped", refreshed, skipped)
    return {"refreshed": refreshed, "skipped": skipped}
