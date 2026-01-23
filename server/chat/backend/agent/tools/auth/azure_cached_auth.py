import json
import logging
import os
import subprocess
from utils.terminal.terminal_run import terminal_run
import time
from typing import Optional, Tuple

from utils.auth.cloud_auth import generate_azure_access_token
from utils.cloud.cloud_utils import get_mode_from_context
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

# Caching controls - use consolidated variables
_AZ_CACHE_ENABLED = os.getenv("AURORA_SETUP_CACHE_ENABLED", "true").lower() == "true"
_AZ_CACHE_TTL = int(os.getenv("AURORA_SETUP_CACHE_TTL", "300"))
_AZ_VERIFY_AZ_CLI = os.getenv("AURORA_VERIFY_CLI_IDENTITY", "false").lower() == "true"

_local_cache: dict[str, tuple[float, dict]] = {}


def _get_cache_client():
    if not _AZ_CACHE_ENABLED:
        return None
    return get_redis_client()


def _cache_key(user_id: str, subscription_id: Optional[str], mode: Optional[str]) -> str:
    sub = subscription_id or "default"
    normalized_mode = (mode or "agent").strip().lower() or "agent"
    return f"cloud_exec:azure_setup:v1:{user_id}:{sub}:{normalized_mode}"


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
            logger.debug(f"Azure cache GET error: {e}")
    return None


def _cache_set(key: str, value: dict) -> None:
    ttl = _AZ_CACHE_TTL
    _local_cache[key] = (time.time() + ttl, value)
    logger.info(f"Azure setup local-cache SET key={key} ttl={ttl}s")
    client = _get_cache_client()
    if client is not None:
        try:
            client.setex(key, ttl, json.dumps(value))
            logger.info(f"Azure setup cache SET (Redis) key={key} ttl={ttl}s")
        except Exception as e:
            logger.debug(f"Azure cache SET error: {e}")


def setup_azure_environment_cached(user_id: str, subscription_id: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """Set up Azure auth with caching. Returns (success, subscription_id, auth_method)."""
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up Azure environment (cached helper)...")

        # Acquire Azure credentials and subscription
        current_mode = get_mode_from_context()
        az_creds = generate_azure_access_token(user_id, subscription_id, mode=current_mode)
        access_token = az_creds["access_token"]  # not used by az login but kept for uniformity
        subscription_id = az_creds["subscription_id"]
        tenant_id = az_creds["tenant_id"]
        client_id = az_creds.get("client_id")
        client_secret = az_creds.get("client_secret")

        if not all([tenant_id, client_id, client_secret]):
            raise ValueError("Incomplete Azure credentials for CLI authentication")

        # Cache check
        key = _cache_key(user_id, subscription_id, current_mode)
        cached = _cache_get(key)
        if cached:
            logger.info("Azure setup cache HIT")
            os.environ["AZURE_CLIENT_ID"] = str(client_id)
            os.environ["AZURE_CLIENT_SECRET"] = str(client_secret)
            os.environ["AZURE_TENANT_ID"] = str(tenant_id)
            return True, subscription_id, "service_principal"

        # Apply env
        os.environ["AZURE_CLIENT_ID"] = str(client_id)
        os.environ["AZURE_CLIENT_SECRET"] = str(client_secret)
        os.environ["AZURE_TENANT_ID"] = str(tenant_id)

        # az login service principal
        try:
            login_cmd = [
                "az", "login", "--service-principal",
                "--username", str(client_id),
                "--password", str(client_secret),
                "--tenant", str(tenant_id),
                "--output", "none",
            ]
            login_result = terminal_run(
                login_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if login_result.returncode != 0:
                logger.error(f"Azure CLI login failed: {login_result.stderr}")
                raise ValueError(f"Azure CLI authentication failed: {login_result.stderr}")
            logger.info("Azure CLI authenticated successfully")
            if subscription_id:
                set_subscription_cmd = ["az", "account", "set", "--subscription", str(subscription_id)]
                sub_result = terminal_run(
                    set_subscription_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if sub_result.returncode == 0:
                    logger.info(f"Azure CLI default subscription set to: {subscription_id}")
                else:
                    logger.warning(f"Failed to set default subscription: {sub_result.stderr}")

            if _AZ_VERIFY_AZ_CLI:
                try:
                    who_cmd = terminal_run(
                        ["az", "account", "show"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if who_cmd.returncode == 0:
                        logger.info("Azure CLI identity verification successful")
                    else:
                        logger.warning(f"Azure CLI identity verification failed: {who_cmd.stderr}")
                except Exception as e:
                    logger.warning(f"Failed to verify Azure CLI identity: {e}")
        except subprocess.TimeoutExpired:
            raise ValueError("Azure CLI authentication timed out")
        except Exception as e:
            raise ValueError(f"Azure CLI setup failed: {e}")

        # Cache success
        try:
            _cache_set(key, {
                "ok": True,
                "subscription_id": subscription_id,
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "mode": current_mode or "agent",
                "ts": time.time(),
            })
        except Exception:
            pass

        logger.info(f"Azure environment configured for subscription: {subscription_id}")
        logger.info(f"TIME: setup_azure (cached helper) completed in {time.perf_counter() - fn_start:.2f}s")
        return True, subscription_id, "service_principal"

    except Exception as e:
        logger.error(f"Failed to setup Azure environment (cached helper): {e}")
        return False, None, None 
