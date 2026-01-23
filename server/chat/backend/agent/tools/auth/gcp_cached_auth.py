import json
import logging
import os
import subprocess
from utils.terminal.terminal_run import terminal_run
import time
from typing import Optional, Tuple

from utils.auth.cloud_auth import generate_contextual_access_token
from utils.cloud.cloud_utils import get_mode_from_context
from utils.cache.redis_client import get_redis_client

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
    
    # Clear from Redis cache
    client = _get_cache_client()
    if client is not None:
        try:
            cleared_redis = 0
            # Get all keys matching the pattern
            pattern = f"cloud_exec:gcp_setup:v1:{user_id}:*"
            keys = client.keys(pattern)
            if keys:
                cleared_redis = client.delete(*keys)
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
    
    # Clean up temporary credentials files
    try:
        import tempfile
        import glob
        temp_dir = tempfile.gettempdir()
        # Look for GCP credentials files (created by create_local_credentials_file)
        pattern = os.path.join(temp_dir, 'gcp_credentials_*.json')
        cred_files = glob.glob(pattern)
        
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


def setup_gcp_impersonation_cached(user_id: str, selected_project_id: Optional[str] = None, provider_preference: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """Set up GCP auth with caching. Returns (success, project_id, auth_method='impersonated')."""
    try:
        fn_start = time.perf_counter()
        logger.info("Attempting impersonated access (cached helper)...")

        current_mode = get_mode_from_context()
        key = _cache_key(user_id, selected_project_id, provider_preference, current_mode)
        cached = _cache_get(key)
        if cached:
            logger.info("GCP setup cache HIT")
            access_token = cached.get("access_token")
            project_id = cached.get("project_id")
            sa_email = cached.get("sa_email")
            # Reapply env vars
            if access_token:
                os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = access_token
                os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN"] = access_token
            if project_id:
                os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
            if sa_email:
                os.environ["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email
                os.environ["CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email
            return True, project_id, "impersonated"

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

        # Set env vars
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = access_token
        os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN"] = access_token
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        os.environ["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email
        os.environ["CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email

        # Configure gcloud (best-effort)
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
            imp_result = terminal_run(
                ["gcloud", "config", "set", "auth/impersonate_service_account", sa_email],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info(f"TIME: gcloud config set impersonate_sa took {time.perf_counter() - imp_start:.2f}s")
            if imp_result.returncode == 0:
                logger.info(f"Configured gcloud to impersonate {sa_email}")
            else:
                logger.warning(f"Failed to configure SA impersonation: {imp_result.stderr}")
        except Exception as e:
            logger.warning(f"Failed to configure gcloud settings: {e}")

        # Cache success with token and identifiers
        try:
            _cache_set(key, {
                "access_token": access_token,
                "project_id": project_id,
                "sa_email": sa_email,
                "ts": time.time(),
                "mode": current_mode or "agent",
            })
        except Exception:
            pass

        logger.info(f"Successfully set up impersonated access for project: {project_id}")
        logger.info(f"TIME: setup_gcp_impersonation (cached helper) completed in {time.perf_counter() - fn_start:.2f}s")
        return True, project_id, "impersonated"

    except Exception as e:
        logger.error(f"Failed to generate SA access token (cached helper): {e}")
        return False, None, None 
