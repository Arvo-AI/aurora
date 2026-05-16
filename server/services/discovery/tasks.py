"""
Celery tasks for scheduled infrastructure discovery.
"""

import logging

from celery_config import celery_app
from utils.auth.stateless_auth import set_rls_context, get_org_id_for_user

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ('gcp', 'aws', 'azure', 'ovh', 'scaleway', 'tailscale', 'kubectl')


def _query_connected_providers(cur, user_id=None, conn=None):
    """Query active cloud provider connections.

    If user_id is given, returns just the provider name strings for that user.
    Otherwise returns (user_id, org_id, provider) rows for all users so the
    caller can deduplicate at the org level.
    Requires conn for cross-org queries to set RLS context per-org.

    Includes org_id fallback so org-shared connections (e.g. AWS accounts
    registered under the org rather than directly under the user) are picked
    up the same way every other connection query in the codebase does.
    """
    if user_id is not None:
        org_id = get_org_id_for_user(user_id)
        if conn:
            set_rls_context(cur, conn, user_id, log_prefix="[Discovery]")
        cur.execute("""
                SELECT DISTINCT provider FROM (
                    SELECT provider FROM user_connections
                    WHERE (user_id = %s OR (org_id = %s AND %s IS NOT NULL)) AND status = 'active' AND provider IN %s
                    UNION
                    SELECT provider FROM user_tokens
                    WHERE (user_id = %s OR (org_id = %s AND %s IS NOT NULL)) AND is_active = true AND provider IN %s
                ) AS connected
            """, (user_id, org_id, org_id, SUPPORTED_PROVIDERS, user_id, org_id, org_id, SUPPORTED_PROVIDERS))
        return [row[0] for row in cur.fetchall()]
    else:
        # No RLS needed — cross-org loop sets RLS per user.
        # Returns (user_id, org_id, provider) so callers can group by org.
        cur.execute(
            "SELECT DISTINCT id, org_id FROM users WHERE org_id IS NOT NULL"
        )
        all_users = cur.fetchall()
        results = []
        for uid, org_id in all_users:
            cur.execute("SET myapp.current_user_id = %s;", (uid,))
            cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            if conn:
                conn.commit()
            cur.execute("""
                SELECT DISTINCT provider FROM (
                    SELECT provider FROM user_connections
                    WHERE (user_id = %s OR org_id = %s) AND status = 'active' AND provider IN %s
                    UNION
                    SELECT provider FROM user_tokens
                    WHERE (user_id = %s OR org_id = %s) AND is_active = true AND provider IN %s
                ) AS connected
            """, (uid, org_id, SUPPORTED_PROVIDERS, uid, org_id, SUPPORTED_PROVIDERS))
            for row in cur.fetchall():
                results.append((uid, org_id, row[0]))
        return results


def _clear_discovery_lock(user_id):
    """Remove the Redis dedup lock after a discovery task finishes."""
    try:
        from utils.cache.redis_client import get_redis_client
        redis_client = get_redis_client()
        if redis_client:
            redis_client.delete(f"discovery:running:{user_id}")
    except Exception as e:
        logger.debug(f"[Discovery] Failed to clear lock for user {user_id}: {e}")


# ---------------------------------------------------------------------------
# Credential failure tracking
# ---------------------------------------------------------------------------

# Number of consecutive all-error runs before a provider connection is
# auto-marked inactive and the user is notified.
_CREDENTIAL_FAIL_THRESHOLD = 3
# Redis TTL for the failure counter — 7 days. If a user fixes credentials and
# a successful run resets the counter, stale keys expire on their own.
_CREDENTIAL_FAIL_TTL_SECONDS = 7 * 24 * 3600

# Error substrings that indicate a credential/auth problem rather than a
# transient infrastructure error. Only these trigger the failure counter so
# that intermittent network blips don't accumulate toward auto-disconnect.
_CREDENTIAL_ERROR_FRAGMENTS = (
    # AWS STS / IAM
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredTokenException",
    "AccessDenied",
    "not authorized to assume role",
    "is not authorized to perform",
    "The security token included in the request is invalid",
    # GCP OAuth
    "Reauthentication is needed",
    "invalid_grant",
    "Token has been expired or revoked",
    "UNAUTHENTICATED",
    # Azure
    "AADSTS",
    "ClientAuthenticationError",
    "Invalid client secret",
)

# Substrings that, if present, override the fragment match above.
# These are errors whose text happens to contain a credential-like fragment
# but are actually billing/feature-tier or configuration issues — not bad tokens.
_CREDENTIAL_ERROR_EXCLUSIONS = (
    # GCP Asset Inventory relationship queries require SCC premium; the response
    # body contains "UNAUTHENTICATED" but the OAuth token itself is valid.
    "premium customers",
    "scc premium",
    "relationship is only supported",
    # AWS Resource Explorer not enabled — the account is fine, just missing the index.
    "not authorized to create indexes",
)


def _is_credential_error(error_msg: str) -> bool:
    """Return True when error_msg looks like an auth/credential failure."""
    lower = error_msg.lower()
    if any(excl in lower for excl in _CREDENTIAL_ERROR_EXCLUSIONS):
        return False
    return any(frag.lower() in lower for frag in _CREDENTIAL_ERROR_FRAGMENTS)


def _record_provider_failure(user_id: str, provider: str) -> int:
    """Increment the consecutive-failure counter for (user_id, provider).

    Returns the updated count, or 0 if Redis is unavailable.
    """
    try:
        from utils.cache.redis_client import get_redis_client
        redis_client = get_redis_client()
        if not redis_client:
            return 0
        key = f"discovery:cred_fail:{user_id}:{provider}"
        count = redis_client.incr(key)
        redis_client.expire(key, _CREDENTIAL_FAIL_TTL_SECONDS)
        return count
    except Exception as e:
        logger.debug("[Discovery] Failed to record provider failure for user %s provider %s: %s", user_id, provider, e)
        return 0


def _reset_provider_failure(user_id: str, provider: str) -> None:
    """Clear the consecutive-failure counter after a successful run."""
    try:
        from utils.cache.redis_client import get_redis_client
        redis_client = get_redis_client()
        if redis_client:
            redis_client.delete(f"discovery:cred_fail:{user_id}:{provider}")
    except Exception as e:
        logger.debug("[Discovery] Failed to reset provider failure for user %s provider %s: %s", user_id, provider, e)


def _mark_provider_inactive(user_id: str, provider: str) -> None:
    """Mark all active connections for (user_id, provider) as inactive.

    Covers both user_connections (AWS/kubectl/OVH/Scaleway/Tailscale) and
    user_tokens (GCP/Azure and other OAuth-backed providers).  Deactivates
    both user-scoped and org-scoped rows so org-shared connections are also
    cleaned up.
    """
    from utils.db.db_utils import connect_to_db_as_admin
    from utils.auth.stateless_auth import set_rls_context
    from datetime import datetime, timezone

    org_id = get_org_id_for_user(user_id)
    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[Discovery:AutoDisconnect]")
            now = datetime.now(timezone.utc)
            cur.execute(
                """
                UPDATE user_connections
                SET status = 'inactive', last_verified_at = %s
                WHERE (user_id = %s OR org_id = %s) AND provider = %s AND status = 'active'
                """,
                (now, user_id, org_id, provider),
            )
            connections_updated = cur.rowcount
            cur.execute(
                """
                UPDATE user_tokens
                SET is_active = false, updated_at = %s
                WHERE (user_id = %s OR org_id = %s) AND provider = %s AND is_active = true
                """,
                (now, user_id, org_id, provider),
            )
            tokens_updated = cur.rowcount
        conn.commit()
        logger.warning(
            "[Discovery] Auto-disconnected user=%s provider=%s after %d consecutive credential failures "
            "(connections=%d tokens=%d)",
            user_id, provider, _CREDENTIAL_FAIL_THRESHOLD, connections_updated, tokens_updated,
        )
    except Exception:
        logger.exception("[Discovery] Failed to auto-disconnect user=%s provider=%s", user_id, provider)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def _handle_provider_errors(user_id: str, provider: str, errors: list) -> None:
    """Evaluate errors from a single provider run and update failure tracking.

    If all errors for this provider look like credential failures and the
    consecutive failure count reaches the threshold, the connection is marked
    inactive so it stops being scanned every hour.
    """
    if not errors:
        _reset_provider_failure(user_id, provider)
        return

    all_credential_errors = all(_is_credential_error(e) for e in errors)
    if not all_credential_errors:
        # Any non-credential run breaks the consecutive credential-failure streak
        # so that a pattern of credential → transient → credential → credential
        # does not accumulate toward auto-disconnect.
        _reset_provider_failure(user_id, provider)
        return

    count = _record_provider_failure(user_id, provider)
    logger.warning(
        "[Discovery] Credential failure #%d/%d for user=%s provider=%s",
        count, _CREDENTIAL_FAIL_THRESHOLD, user_id, provider,
    )
    if count >= _CREDENTIAL_FAIL_THRESHOLD:
        _mark_provider_inactive(user_id, provider)
        _reset_provider_failure(user_id, provider)


def _wait_for_gcp_post_auth(user_id, timeout=300, poll_interval=10):
    """Wait for any active GCP post-auth setup task to complete.

    The post-auth task enables APIs and propagates service accounts across all
    projects.  If discovery starts before that finishes, gcloud commands will
    fail with permission / API-not-enabled errors.
    """
    import time
    from celery_config import celery_app as _app

    inspect = _app.control.inspect(timeout=5)
    start = time.time()

    while time.time() - start < timeout:
        try:
            # Check active tasks across all workers
            active = inspect.active() or {}
            found = False
            for _worker, tasks in active.items():
                for t in tasks:
                    if (t.get("name") == "connectors.gcp_connector.gcp_post_auth_tasks.gcp_post_auth_setup_task"
                            and _task_belongs_to_user(t, user_id)):
                        found = True
                        break
                if found:
                    break

            if not found:
                logger.info(f"[Discovery] No active GCP post-auth task for user {user_id}, proceeding")
                return

            logger.info(f"[Discovery] GCP post-auth still running for user {user_id}, waiting {poll_interval}s...")
            time.sleep(poll_interval)
        except Exception as e:
            logger.warning(f"[Discovery] Error checking post-auth status: {e}")
            # If we can't inspect, wait a bit and try again
            time.sleep(poll_interval)

    logger.warning(f"[Discovery] Timed out waiting for GCP post-auth after {timeout}s, proceeding anyway")


def _task_belongs_to_user(task_info, user_id):
    """Check if a Celery task info dict has user_id as its first argument."""
    args = task_info.get("args", [])
    if args:
        return str(args[0]) == str(user_id)
    kwargs = task_info.get("kwargs", {})
    return str(kwargs.get("user_id", "")) == str(user_id)


def _get_all_gcp_project_ids(user_id):
    """Get all GCP project IDs accessible to the user.

    Handles both OAuth and service-account auth types.
    - OAuth: enumerates projects via the Cloud Resource Manager API.
    - Service account: reads the accessible_projects list stored at
      connection time (no extra API call required).

    Returns:
        Tuple of (project_ids: list[str], credential_error: str | None).
        credential_error is set when a known auth failure prevented enumeration
        so the caller can feed it into _handle_provider_errors.
    """
    try:
        from utils.auth.token_management import get_token_data
        from connectors.gcp_connector.auth.service_accounts import (
            get_gcp_auth_type, GCP_AUTH_TYPE_SA,
        )
        from connectors.gcp_connector.billing import has_active_billing

        token_data = get_token_data(user_id, "gcp")
        if not token_data:
            logger.warning("[Discovery] No GCP credentials found for user %s", user_id)
            return [], None

        if get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_SA:
            # SA connections store the project list at connection time.
            accessible = token_data.get("accessible_projects") or []
            project_ids = [
                p.get("project_id") or p.get("projectId")
                for p in accessible
                if isinstance(p, dict)
            ]
            project_ids = [pid for pid in project_ids if pid]
            if not project_ids:
                # Fall back to the single default project stored on the token.
                default = token_data.get("default_project_id")
                if default:
                    project_ids = [default]
            logger.info("[Discovery] Found %d GCP projects (SA) for user %s", len(project_ids), user_id)
            return project_ids, None

        # OAuth path — proactively refresh the access token before use so that
        # a near-expired token doesn't cause the enumeration call to fail. If
        # the refresh itself fails (dead refresh token, revoked consent, etc.)
        # we surface the error string so it feeds into the credential failure
        # tracker rather than being silently swallowed by get_credentials().
        from utils.auth.token_refresh import refresh_token_if_needed as _refresh_gcp

        refreshed = _refresh_gcp(user_id, "gcp")
        if refreshed is None:
            # Refresh failed — refresh token is dead or missing. Return a
            # recognisable error string so _handle_provider_errors can count
            # this as a credential failure and eventually auto-disconnect.
            err = "GCP token refresh failed: Reauthentication is needed. Please reconnect your GCP account."
            logger.warning("[Discovery] %s (user=%s)", err, user_id)
            return [], err

        # Enumerate via Cloud Resource Manager API.
        # Pass the already-refreshed token data directly — avoids a second
        # Vault round-trip that get_credentials_from_db would otherwise make.
        from connectors.gcp_connector.auth_compatibility import get_credentials, get_project_list

        credentials = get_credentials(refreshed)
        projects = get_project_list(credentials)

        project_ids = []
        for p in projects:
            pid = p.get("projectId")
            if not pid:
                continue
            try:
                if has_active_billing(pid, credentials):
                    project_ids.append(pid)
            except Exception:
                project_ids.append(pid)

        logger.info("[Discovery] Found %d GCP projects (OAuth) for user %s", len(project_ids), user_id)
        return project_ids, None
    except Exception as e:
        logger.error("[Discovery] Failed to enumerate GCP projects for user %s: %s", user_id, e)
        return [], str(e)


@celery_app.task(name="services.discovery.tasks.run_full_discovery", bind=True, max_retries=0)
def run_full_discovery(self):
    """Run full infrastructure discovery for all orgs with connected cloud providers.

    Scheduled by Celery beat to run every hour.
    Can also be triggered on-demand via POST /api/graph/discover.

    Runs once per org rather than once per user. All org members share the
    same cloud connectors, so running discovery N times for N members would
    produce identical results at N× the cost and latency.  The credential
    owner for each provider is used as the representative so that Vault
    lookups and STS/OAuth refreshes resolve against the correct stored token.
    """
    from utils.db.db_utils import connect_to_db_as_admin
    from utils.secrets.secret_ref_utils import get_token_owner_id as _get_owner
    from services.discovery.discovery_service import run_discovery_for_user

    logger.info("[Discovery Task] Starting full discovery run")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()  # No RLS needed — cross-org loop, sets RLS per user inside
        rows = _query_connected_providers(cur, conn=conn)
        cur.close()
        conn.close()

        if not rows:
            logger.info("[Discovery Task] No orgs with connected cloud providers")
            return {"status": "no_users", "users_processed": 0}

        # Deduplicate by org: collect the union of active providers per org and
        # keep one representative user (the first encountered, which is
        # consistent within a run since _query_connected_providers iterates
        # users in a stable order).  Provider credential lookups use the token
        # owner at runtime, so the representative just needs to belong to the org.
        orgs: dict = {}  # org_id -> {"rep": user_id, "providers": {provider: {}}}
        for user_id, org_id, provider in rows:
            if org_id not in orgs:
                orgs[org_id] = {"rep": user_id, "providers": {}}
            orgs[org_id]["providers"][provider] = {}

        logger.info("[Discovery Task] Processing %d org(s)", len(orgs))

        results = []
        for org_id, org_info in orgs.items():
            user_id = org_info["rep"]
            providers = org_info["providers"]
            try:
                if "gcp" in providers:
                    # Resolve the GCP credential owner so token refresh and
                    # Vault reads go against the correct row.
                    owner_id = _get_owner(user_id, "gcp")
                    _wait_for_gcp_post_auth(owner_id)
                    gcp_project_ids, gcp_cred_error = _get_all_gcp_project_ids(owner_id)
                    if gcp_project_ids:
                        providers["gcp"] = {"project_ids": gcp_project_ids}
                    else:
                        from utils.auth.stateless_auth import get_user_preference
                        root_project = get_user_preference(owner_id, "gcp_root_project")
                        if root_project:
                            providers["gcp"] = {"project_ids": [root_project]}
                        else:
                            del providers["gcp"]
                            if gcp_cred_error:
                                _handle_provider_errors(owner_id, "gcp", [gcp_cred_error])
                            logger.warning(
                                "[Discovery Task] No GCP projects for org=%s owner=%s — skipping GCP",
                                org_id, owner_id,
                            )

                if not providers:
                    logger.info("[Discovery Task] No valid providers for org=%s, skipping", org_id)
                    continue

                summary = run_discovery_for_user(user_id, providers)
                summary["org_id"] = org_id
                results.append(summary)
                logger.info(
                    "[Discovery Task] Org %s (rep=%s): %d nodes discovered",
                    org_id, user_id, summary.get("phase1_nodes", 0),
                )

                # Use provider_errors from the summary — accurately tagged at
                # the source, no string matching. Always iterate all providers
                # so that a clean run resets any prior failure counter.
                provider_errors = summary.get("provider_errors", {})
                for pname in providers:
                    # Always resolve to the credential owner so that the Redis
                    # key namespace is consistent: failures and resets both use
                    # tracking_id, preventing a clean run from resetting under
                    # the wrong (representative) user_id.
                    tracking_id = _get_owner(user_id, pname)
                    pname_errors = provider_errors.get(pname, [])
                    _handle_provider_errors(tracking_id, pname, pname_errors)

            except Exception:
                logger.exception("[Discovery Task] Failed for org=%s rep=%s", org_id, user_id)
                results.append({"org_id": org_id, "user_id": user_id, "error": "see logs"})

        return {
            "status": "completed",
            "orgs_processed": len(orgs),
            "users_processed": len(orgs),  # kept for backwards-compat with callers reading this key
            "results": results,
        }

    except Exception as e:
        logger.error(f"[Discovery Task] Fatal error: {e}")
        return {"status": "error", "error": str(e)}


@celery_app.task(
    name="services.discovery.tasks.run_user_discovery",
    bind=True,
    max_retries=0,
    soft_time_limit=7200,
    time_limit=10800,
)
def run_user_discovery(self, user_id):
    """Run discovery for a single user. Called on-demand via API."""
    from celery.exceptions import SoftTimeLimitExceeded
    from utils.db.db_utils import connect_to_db_as_admin
    from services.discovery.discovery_service import run_discovery_for_user

    logger.info(f"[Discovery Task] Starting on-demand discovery for user {user_id}")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()
        set_rls_context(cur, conn, user_id, log_prefix="[Discovery Task]")
        provider_names = _query_connected_providers(cur, user_id, conn=conn)

        if not provider_names:
            cur.close()
            conn.close()
            return {"status": "no_providers", "user_id": user_id}

        # Build credentials dict per provider from user_preferences
        providers = {name: {} for name in provider_names}

        if "gcp" in providers:
            # Fetch root project while we still have the cursor
            from utils.auth.stateless_auth import get_user_preference
            root_project = get_user_preference(user_id, 'gcp_root_project')

        # Query active kubectl clusters for this user
        cur.execute("""
            SELECT c.cluster_id, t.cluster_name
            FROM active_kubectl_connections c
            JOIN kubectl_agent_tokens t ON c.token = t.token
            WHERE t.user_id = %s AND t.status = 'active' AND c.status = 'active'
        """, (user_id,))
        kubectl_rows = cur.fetchall()

        # Close DB connection BEFORE calling setup functions that also use the pool
        cur.close()
        conn.close()

        # Add kubectl provider if there are active clusters
        if kubectl_rows:
            clusters = [
                {"cluster_id": row[0], "cluster_name": row[1] or row[0]}
                for row in kubectl_rows
            ]
            providers["kubectl"] = {"clusters": clusters}
            logger.info(f"[Discovery Task] Found {len(clusters)} active kubectl clusters for user {user_id}")

        if "gcp" in providers:
            # Wait for GCP post-auth setup to finish (API enablement, SA propagation)
            _wait_for_gcp_post_auth(user_id)

            # Fetch ALL project IDs so discovery covers every project, not just root
            gcp_project_ids, _ = _get_all_gcp_project_ids(user_id)
            if gcp_project_ids:
                providers["gcp"] = {"project_ids": gcp_project_ids}
            elif root_project:
                providers["gcp"] = {"project_ids": [root_project]}

        summary = run_discovery_for_user(user_id, providers)
        return summary

    except SoftTimeLimitExceeded:
        logger.error(f"[Discovery Task] Soft time limit exceeded for user {user_id}")
        return {"status": "error", "user_id": user_id, "error": "Discovery timed out"}
    except Exception as e:
        logger.error(f"[Discovery Task] Failed for user {user_id}: {e}")
        return {"status": "error", "user_id": user_id, "error": str(e)}
    finally:
        _clear_discovery_lock(user_id)


@celery_app.task(name="services.discovery.tasks.mark_stale_services", bind=True, max_retries=0, soft_time_limit=300, time_limit=600)
def mark_stale_services(self):
    """Mark services not updated in 7 days as stale, and delete those older than 30 days.

    Runs daily at 3 AM. The 30-day deletion acts as a safety net for nodes
    that were never cleaned up by a disconnect event.
    """
    from utils.db.db_utils import connect_to_db_as_admin
    from services.graph.memgraph_client import get_memgraph_client

    logger.info("[Discovery Task] Starting stale service detection")

    try:
        conn = connect_to_db_as_admin()
        cur = conn.cursor()  # No RLS needed — cross-org loop, sets RLS per user inside
        rows = _query_connected_providers(cur, conn=conn)
        cur.close()
        conn.close()
        # Deduplicate by org: one representative user per org is sufficient
        # since graph data is written per org credential owner.
        user_ids = list({org_id: user_id for user_id, org_id, _provider in rows}.values())

        client = get_memgraph_client()
        total_marked = 0
        total_deleted = 0
        for user_id in user_ids:
            try:
                marked = client.mark_stale_services(user_id, stale_days=7)
                total_marked += marked
                if marked > 0:
                    logger.info(f"[Discovery Task] Marked {marked} stale services for user {user_id}")
            except Exception as e:
                logger.error(f"[Discovery Task] Stale detection failed for user {user_id}: {e}")
            try:
                deleted = client.delete_stale_services(user_id, stale_days=30)
                total_deleted += deleted
                if deleted > 0:
                    logger.info(f"[Discovery Task] Deleted {deleted} stale services (>30d) for user {user_id}")
            except Exception as e:
                logger.exception(f"[Discovery Task] Stale deletion failed for user {user_id}: {e}")

        logger.info(f"[Discovery Task] Stale detection complete: {total_marked} marked, {total_deleted} deleted")
        return {"status": "completed", "total_marked": total_marked, "total_deleted": total_deleted}

    except Exception as e:
        logger.error(f"[Discovery Task] Stale detection fatal error: {e}")
        return {"status": "error", "error": str(e)}
