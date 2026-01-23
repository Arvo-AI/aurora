import json
import logging
import os
import subprocess
from utils.terminal.terminal_run import terminal_run
import time
from typing import Optional, Tuple

from utils.auth.stateless_auth import get_credentials_from_db
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

# Caching controls - use consolidated variables
_AWS_CACHE_ENABLED = os.getenv("AURORA_SETUP_CACHE_ENABLED", "true").lower() == "true"
_AWS_CACHE_TTL = int(os.getenv("AURORA_SETUP_CACHE_TTL", "300"))  # seconds
_AWS_VERIFY_CLI_IDENTITY = os.getenv("AURORA_VERIFY_CLI_IDENTITY", "false").lower() == "true"

_aws_local_cache: dict[str, tuple[float, dict]] = {}


def _get_cache_client():
    if not _AWS_CACHE_ENABLED:
        return None
    return get_redis_client()


def _cache_key(user_id: str, access_key_id: str, region: str) -> str:
    return f"cloud_exec:aws_setup:v1:{user_id}:{access_key_id}:{region}"


def _cache_get(key: str) -> Optional[dict]:
    client = _get_cache_client()
    now = time.time()
    if client is not None:
        try:
            raw = client.get(key)
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    return None
        except Exception as e:
            logger.debug(f"AWS cache GET error: {e}")
    entry = _aws_local_cache.get(key)
    if entry:
        expires_at, value = entry
        if now < expires_at:
            return value
        _aws_local_cache.pop(key, None)
    return None


def _cache_set(key: str, value: dict) -> None:
    client = _get_cache_client()
    ttl = _AWS_CACHE_TTL
    if client is not None:
        try:
            client.setex(key, ttl, json.dumps(value))
            logger.info(f"AWS setup cache SET key={key} ttl={ttl}s")
            return
        except Exception as e:
            logger.debug(f"AWS cache SET error: {e}")
    _aws_local_cache[key] = (time.time() + ttl, value)
    logger.info(f"AWS setup local-cache SET key={key} ttl={ttl}s")


def setup_aws_credentials_cached(user_id: str, selected_region: Optional[str] = None, return_isolated: bool = False) -> Tuple[bool, Optional[str], Optional[str], Optional[dict]]:
    """Setup AWS auth with caching. 
    
    Returns (success, region, auth_method, isolated_env) if return_isolated=True
    Returns (success, region, auth_method) if return_isolated=False (backward compat)
    
    When return_isolated=True, returns an isolated environment dict instead of modifying os.environ.
    """
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up AWS credentials (cached helper)...")

        # Fetch credentials
        creds_start = time.perf_counter()
        aws_credentials = get_credentials_from_db(user_id, "aws")
        logger.info(f"TIME: get_credentials_from_db took {time.perf_counter() - creds_start:.2f}s")
        if not aws_credentials:
            logger.error(f"No AWS credentials found for user {user_id}")
            return (False, None, None, None) if return_isolated else (False, None, None)

        # Validate
        access_key_id = aws_credentials.get('aws_access_key_id')
        secret_access_key = aws_credentials.get('aws_secret_access_key')
        if not access_key_id or not secret_access_key:
            logger.error("Missing required AWS credential fields")
            return (False, None, None, None) if return_isolated else (False, None, None)

        # Resolve region
        regions = aws_credentials.get('aws_regions', ['us-east-1'])
        if isinstance(regions, list) and regions:
            region = selected_region if selected_region in regions else regions[0]
        else:
            region = selected_region or 'us-east-1'
        logger.info(f"Using AWS region: {region}")

        # Build isolated environment (always needed for return_isolated mode)
        isolated_env = None
        if return_isolated:
            isolated_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "USER": os.environ.get("USER", ""),
                "AWS_ACCESS_KEY_ID": access_key_id,
                "AWS_SECRET_ACCESS_KEY": secret_access_key,
                "AWS_DEFAULT_REGION": region,
                "AWS_REGION": region,
            }

        # Cache check
        key = _cache_key(user_id, access_key_id, region)
        cached = _cache_get(key)
        if cached:
            logger.info(f"AWS setup cache HIT for user={user_id} region={region}")
            if return_isolated:
                # Return isolated environment without modifying os.environ
                return True, region, "access_key", isolated_env
            else:
                # Legacy mode: modify os.environ for backward compatibility
                # Clear any existing AWS profile environment variables to avoid conflicts
                for key in ["AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]:
                    if key in os.environ:
                        del os.environ[key]
                os.environ["AWS_ACCESS_KEY_ID"] = access_key_id
                os.environ["AWS_SECRET_ACCESS_KEY"] = secret_access_key
                os.environ["AWS_DEFAULT_REGION"] = region
                os.environ["AWS_REGION"] = region
                return True, region, "access_key"

        # STS validation - always use explicit credentials for thread safety
        import boto3
        from botocore.config import Config
        
        sts_start = time.perf_counter()
        
        # Create a config to avoid profile issues
        config = Config(
            region_name=region,
            signature_version='v4',
            retries={'max_attempts': 3}
        )
        
        # Use explicit credentials with boto3.client to avoid profile lookup
        sts = boto3.client(
            'sts',
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=config
        )
        identity = sts.get_caller_identity()
        logger.info(f"TIME: AWS STS validation took {time.perf_counter() - sts_start:.2f}s")
        account_id = identity['Account']
        user_arn = identity.get('Arn', 'Unknown')
        logger.info(f"Successfully validated AWS credentials for account: {account_id}")
        logger.info(f"User ARN: {user_arn}")
        
        # Cache the successful validation
        _cache_set(key, {"success": True})
        
        # Return based on mode
        if return_isolated:
            # Return isolated environment without modifying os.environ
            logger.info(f"AWS credentials configured (isolated mode) for region: {region}")
            return True, region, "access_key", isolated_env
        else:
            # Legacy mode: modify os.environ for backward compatibility
            # Clear any existing AWS profile environment variables to avoid conflicts
            for key in ["AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]:
                if key in os.environ:
                    del os.environ[key]
            
            # Apply env
            os.environ["AWS_ACCESS_KEY_ID"] = access_key_id
            os.environ["AWS_SECRET_ACCESS_KEY"] = secret_access_key
            os.environ["AWS_DEFAULT_REGION"] = region
            os.environ["AWS_REGION"] = region
            if "AWS_SESSION_TOKEN" in os.environ:
                del os.environ["AWS_SESSION_TOKEN"]

            # Configure AWS CLI default region (best-effort) - only in legacy mode
            try:
                config_start = time.perf_counter()
                region_cmd = ["aws", "configure", "set", "region", region]
                region_result = terminal_run(
                    region_cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.info(f"TIME: aws configure set region took {time.perf_counter() - config_start:.2f}s")
                if region_result.returncode == 0:
                    logger.info(f"Successfully set default AWS region: {region}")
                else:
                    logger.warning(f"Failed to set default AWS region: {region_result.stderr}")

                if _AWS_VERIFY_CLI_IDENTITY:
                    try:
                        identity_start = time.perf_counter()
                        identity_cmd = terminal_run(
                            ['aws', 'sts', 'get-caller-identity'],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            env=os.environ.copy(),
                        )
                        logger.info(f"TIME: aws sts get-caller-identity took {time.perf_counter() - identity_start:.2f}s")
                        if identity_cmd.returncode == 0:
                            logger.info(f"AWS CLI identity verification successful: {identity_cmd.stdout.strip()}")
                        else:
                            logger.warning(f"AWS CLI identity verification failed: {identity_cmd.stderr}")
                    except Exception as e:
                        logger.warning(f"Failed to verify AWS CLI configuration: {e}")
            except Exception as e:
                logger.warning(f"Failed to configure AWS CLI settings: {e}")

            logger.info(f"Successfully set up AWS credentials for region: {region}")
            logger.info(f"TIME: setup_aws_credentials (cached helper) completed in {time.perf_counter() - fn_start:.2f}s")
            return True, region, "access_key"

    except Exception as e:
        logger.error(f"Failed to set up AWS credentials (cached helper): {e}")
        return (False, None, None, None) if return_isolated else (False, None, None)


def setup_aws_credentials_cached_isolated(user_id: str, selected_region: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[str], Optional[dict]]:
    """Concurrent-safe version that always returns isolated environment.
    
    Returns (success, region, auth_method, isolated_env).
    This is a convenience wrapper that always uses return_isolated=True.
    """
    return setup_aws_credentials_cached(user_id, selected_region, return_isolated=True) 