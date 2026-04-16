import json
import logging
import os
import subprocess
from utils.terminal.terminal_run import terminal_run
import time
from typing import Literal, Optional, Tuple

from utils.auth.cloud_auth import generate_contextual_access_token
from utils.cloud.cloud_utils import get_mode_from_context
from utils.cache.redis_client import get_redis_client
from connectors.gcp_connector.auth import GCP_AUTH_TYPE_SA

GcpAuthMethod = Literal["impersonated", "service_account"]

logger = logging.getLogger(__name__)

# Caching controls - use consolidated variables
_GCP_CACHE_ENABLED = os.getenv("AURORA_SETUP_CACHE_ENABLED", "true").lower() == "true"
_GCP_CACHE_TTL = int(os.getenv("AURORA_SETUP_CACHE_TTL", "300"))  # seconds
_GCP_CACHE_TOKEN_IN_REDIS = os.getenv("AURORA_CACHE_TOKEN_IN_REDIS", "false").lower() == "true"

_local_cache: dict[str, tuple[float, dict]] = {}


def _get_cache_client():
    if not _GCP_CACHE_ENABLED or not _GCP_CACHE_TOKEN_IN_REDIS:
        return None
    return get_redis_client()


def _cache_key(user_id: str, selected_project_id: Optional[str], provider_pref: Optional[str], mode: Optional[str]) -> str:
    sel = selected_project_id or "default"
    prov = provider_pref or "auto"
    normalized_mode = (mode or "agent").strip().lower() or "agent"
    return f"cloud_exec:gcp_setup:v1:{user_id}:{sel}:{prov}:{normalized_mode}"


def _cache_get(key: str) -> Optional[dict]:
    now = time.time()
    entry = _local_cache.get(key)
    if entry:
        exp, val = entry
        if now < exp:
            return val
        _local_cache.pop(key, None)
    client = _get_cache_client()
    if client is not None:
        try:
            raw = client.get(key)
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    return None
        except Exception as e:
            logger.debug(f"GCP cache GET error: {e}")
    return None


def _cache_set(key: str, value: dict) -> None:
    ttl = _GCP_CACHE_TTL
    # Always set in local cache
    _local_cache[key] = (time.time() + ttl, value)
    logger.info(f"GCP setup local-cache SET key={key} ttl={ttl}s")
    # Optionally set in Redis (token included)
    client = _get_cache_client()
    if client is not None:
        try:
            client.setex(key, ttl, json.dumps(value))
            logger.info(f"GCP setup cache SET (Redis) key={key} ttl={ttl}s")
        except Exception as e:
            logger.debug(f"GCP cache SET error: {e}")


def clear_gcp_cache_for_user(user_id: str) -> None:
    """
    Clear all GCP cached credentials for a specific user.
    Called when user disconnects from GCP to ensure no stale credentials remain.
    """
    logger.info(f"Clearing GCP cache for user {user_id}")
    
    # Clear all possible cache key variations for this user
    # We need to clear all project_id and provider combinations
    cache_patterns = [
        _cache_key(user_id, None, None, None),  # default/auto
        _cache_key(user_id, "default", "auto", None),
        _cache_key(user_id, "default", "gcp", None),
        _cache_key(user_id, None, "gcp", None),
    ]
    
    # Clear from local in-memory cache
    cleared_local = 0
    for key in list(_local_cache.keys()):
        if user_id in key:  # Clear any key containing this user_id
            _local_cache.pop(key, None)
            cleared_local += 1
    
    if cleared_local > 0:
        logger.info(f"Cleared {cleared_local} entries from local GCP cache for user {user_id}")
    
    # Clear from Redis cache. Use SCAN (non-blocking, cursor-based) instead
    # of KEYS which is O(N) over the whole keyspace and blocks the Redis
    # server — a well-known production footgun.
    client = _get_cache_client()
    if client is not None:
        try:
            cleared_redis = 0
            pattern = f"cloud_exec:gcp_setup:v1:{user_id}:*"
            batch: list = []
            for key in client.scan_iter(match=pattern, count=500):
                batch.append(key)
                if len(batch) >= 500:
                    cleared_redis += client.delete(*batch)
                    batch.clear()
            if batch:
                cleared_redis += client.delete(*batch)
            if cleared_redis:
                logger.info(f"Cleared {cleared_redis} entries from Redis GCP cache for user {user_id}")
        except Exception as e:
            logger.warning(f"Error clearing Redis GCP cache for user {user_id}: {e}")
    
    # Clear environment variables if they were set for this user
    env_vars_to_clear = [
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "GOOGLE_CLOUD_PROJECT",
        "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
        "CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT",
        "GOOGLE_APPLICATION_CREDENTIALS"
    ]
    
    for var in env_vars_to_clear:
        if var in os.environ:
            os.environ.pop(var, None)
            logger.debug(f"Cleared environment variable: {var}")
    
    # Also drop any cached SA ADC file for this user from cloud_exec_tool's
    # in-memory cache so the next tool call rewrites it from the fresh Vault
    # payload.
    try:
        from chat.backend.agent.tools.cloud_exec_tool import _sa_adc_file_cache
        stale_path = _sa_adc_file_cache.pop(user_id, None)
        if stale_path and os.path.exists(stale_path):
            try:
                os.remove(stale_path)
            except Exception as e:
                logger.debug(f"Could not remove cached SA ADC file {stale_path}: {e}")
    except Exception as e:
        logger.debug(f"Could not clear SA ADC file cache for user {user_id}: {e}")

    # Clean up temporary credentials files
    try:
        import tempfile
        import glob
        temp_dir = tempfile.gettempdir()
        # Cover both the OAuth-mode authorized_user file and the SA-mode files
        # (including the cloud_exec_tool ADC file prefix).
        patterns = [
            os.path.join(temp_dir, 'gcp_credentials_*.json'),
            os.path.join(temp_dir, 'gcp_sa_credentials_*.json'),
            os.path.join(temp_dir, 'gcp_sa_adc_*.json'),
        ]
        cred_files: list[str] = []
        for pattern in patterns:
            cred_files.extend(glob.glob(pattern))
        
        cleaned_files = 0
        for cred_file in cred_files:
            try:
                # Only delete files older than 5 minutes to avoid deleting active credentials
                if os.path.exists(cred_file):
                    file_age = time.time() - os.path.getmtime(cred_file)
                    if file_age > 300:  # 5 minutes
                        os.remove(cred_file)
                        cleaned_files += 1
            except Exception as e:
                logger.debug(f"Could not remove temp credentials file {cred_file}: {e}")
        
        if cleaned_files > 0:
            logger.info(f"Cleaned up {cleaned_files} temporary GCP credentials files")
    except Exception as e:
        logger.debug(f"Error cleaning up temporary credentials files: {e}")
    
    logger.info(f"Successfully cleared all GCP caches for user {user_id}")


def _apply_gcp_env(
    access_token: Optional[str],
    project_id: Optional[str],
    sa_email: Optional[str],
    is_sa_mode: bool,
) -> None:
    """Apply the GCP auth env vars for the current process.

    In SA mode, `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` and its alias are
    explicitly cleared: the uploaded SA IS the working identity, and telling
    gcloud to impersonate it against itself requires a non-default
    `iam.serviceAccounts.getAccessToken` binding on self — fails in the
    common case. The access token alone is sufficient for gcloud to
    authenticate.
    """
    if access_token:
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = access_token
        os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN"] = access_token
    if project_id:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    if is_sa_mode:
        os.environ.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
        os.environ.pop("CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT", None)
    elif sa_email:
        os.environ["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email
        os.environ["CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email


def setup_gcp_impersonation_cached(
    user_id: str,
    selected_project_id: Optional[str] = None,
    provider_preference: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[GcpAuthMethod]]:
    """Set up GCP auth with caching.

    Returns (success, project_id, auth_method) where auth_method is
    'impersonated' for OAuth users (Aurora's per-user SA impersonation chain)
    or 'service_account' for users who uploaded their own SA key directly.
    """
    try:
        fn_start = time.perf_counter()
        logger.info("Attempting GCP access setup (cached helper)...")

        current_mode = get_mode_from_context()
        key = _cache_key(user_id, selected_project_id, provider_preference, current_mode)
        cached = _cache_get(key)
        if cached:
            logger.info("GCP setup cache HIT")
            cached_is_sa = cached.get("auth_type") == GCP_AUTH_TYPE_SA
            _apply_gcp_env(
                cached.get("access_token"),
                cached.get("project_id"),
                cached.get("sa_email"),
                is_sa_mode=cached_is_sa,
            )
            return True, cached.get("project_id"), ("service_account" if cached_is_sa else "impersonated")

        token_start = time.perf_counter()
        token_resp = generate_contextual_access_token(
            user_id,
            selected_project_id=selected_project_id,
            override_provider=provider_preference,
            mode=current_mode,
        )
        logger.info(f"TIME: generate_contextual_access_token took {time.perf_counter() - token_start:.2f}s")
        access_token = token_resp["access_token"]
        project_id = token_resp["project_id"]
        sa_email = token_resp["service_account_email"]
        is_sa_mode = token_resp.get("auth_type") == GCP_AUTH_TYPE_SA
        auth_method: GcpAuthMethod = "service_account" if is_sa_mode else "impersonated"

        _apply_gcp_env(access_token, project_id, sa_email, is_sa_mode=is_sa_mode)

        # Configure gcloud (best-effort). Always set the default project; the
        # impersonation config is set in OAuth mode and explicitly unset in SA
        # mode so gcloud's persistent config (via $CLOUDSDK_CONFIG) can't
        # carry stale impersonation state across processes.
        try:
            config_start = time.perf_counter()
            proj_result = terminal_run(
                ["gcloud", "config", "set", "project", project_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info(f"TIME: gcloud config set project took {time.perf_counter() - config_start:.2f}s")
            if proj_result.returncode == 0:
                logger.info(f"Successfully set default project: {project_id}")
            else:
                logger.warning(f"Failed to set default project: {proj_result.stderr}")

            imp_start = time.perf_counter()
            if is_sa_mode:
                imp_result = terminal_run(
                    ["gcloud", "config", "unset", "auth/impersonate_service_account"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.info(f"TIME: gcloud config unset impersonate_sa took {time.perf_counter() - imp_start:.2f}s")
                if imp_result.returncode == 0:
                    logger.info("Cleared gcloud auth/impersonate_service_account (SA mode)")
                else:
                    # Non-fatal: the config key may simply not be set.
                    logger.debug(f"gcloud config unset impersonate_sa returned non-zero: {imp_result.stderr}")
            else:
                imp_result = terminal_run(
                    ["gcloud", "config", "set", "auth/impersonate_service_account", sa_email],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.info("TIME: gcloud config set impersonate_sa took %.2fs", time.perf_counter() - imp_start)
                if imp_result.returncode == 0:
                    logger.info("Configured gcloud to impersonate the per-user Aurora service account")
                else:
                    logger.warning("Failed to configure gcloud SA impersonation (returncode=%s)", imp_result.returncode)
        except Exception as e:
            logger.warning("Failed to configure gcloud settings (error_type=%s)", type(e).__name__)

        try:
            _cache_set(key, {
                "access_token": access_token,
                "project_id": project_id,
                "sa_email": sa_email,
                "auth_type": auth_method,
                "ts": time.time(),
                "mode": current_mode or "agent",
            })
        except Exception as e:
            logger.debug("Failed to persist GCP setup cache entry (error_type=%s)", type(e).__name__)

        logger.info("Successfully set up GCP access (%s)", auth_method)
        logger.info("TIME: setup_gcp_impersonation (cached helper) completed in %.2fs", time.perf_counter() - fn_start)
        return True, project_id, auth_method

    except Exception as e:
        logger.error(f"Failed to generate GCP access token (cached helper): {e}")
        return False, None, None
