import json
import logging
import os
import re
import shlex
import subprocess
from utils.terminal.terminal_run import terminal_run
import time  # Added for performance timing
import requests
import tiktoken
from typing import Dict, Any, Optional, Tuple
from langchain_core.tools import StructuredTool
from pathlib import Path

from utils.auth.cloud_auth import generate_contextual_access_token
from utils.auth.cloud_auth import generate_azure_access_token
from .output_sanitizer import sanitize_command_output, filter_error_messages, truncate_json_fields
from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation
from .cloud_provider_utils import determine_target_provider_from_context
from chat.backend.agent.access import ModeAccessController
from utils.cloud.cloud_utils import get_mode_from_context


# --------------------------------------------------------------------------------------
# Token counting utility
# --------------------------------------------------------------------------------------
def count_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens in text using tiktoken for the specified model."""
    try:
        encoding = tiktoken.encoding_for_model(model)
        return len(encoding.encode(text))
    except KeyError:
        # Fallback to cl100k_base encoding if model not found
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))

# --------------------------------------------------------------------------------------
# Common verb sets used across providers
# --------------------------------------------------------------------------------------
_READ_ONLY_VERBS: set[str] = {
    "list", "describe", "get", "show", "config", "version", "info", "view", "read", "status",
}
# A non-exhaustive but conservative set of verbs considered to CHANGE state
_ACTION_VERBS: set[str] = {
    "create", "delete", "update", "apply", "destroy", "terminate", "start", "stop",
    "restart", "attach", "detach", "enable", "disable", "put", "remove",
}


def summarize_cloud_command(cmd: str) -> str:
    """Return a short description of what the CLI command (gcloud/az/aws) intends to do."""

    try:
        tokens = shlex.split(cmd)
    except Exception:
        tokens = cmd.split()

    action = None
    resource_type = None
    resource_name = None
    zone_or_region = None

    # Tokens representing the CLI executable that we should skip when searching
    cli_tokens = {"gcloud", "az", "aws", "kubectl", "gsutil", "bq", "scw", "ovh"}

    for i, tok in enumerate(tokens):
        low = tok.lower()

        # Detect action verb
        if low in _ACTION_VERBS.union(_READ_ONLY_VERBS):
            action = low

            # Try to infer resource type – walk backwards until we find a token
            # that is not a flag or CLI executable.
            j = i - 1
            while j >= 0 and (tokens[j].lower() in cli_tokens or tokens[j].startswith("-")):
                j -= 1
            if j >= 0:
                resource_type = tokens[j]

            # Resource name usually follows the action verb (unless it's a flag)
            k = i + 1
            while k < len(tokens) and tokens[k].startswith("-"):
                k += 1
            if k < len(tokens):
                resource_name = tokens[k]

        # Capture location / region / zone flags for common CLIs
        if low.startswith("--zone="):
            zone_or_region = tok.split("=", 1)[1]
        elif low == "--zone" and i + 1 < len(tokens):
            zone_or_region = tokens[i + 1]
        elif low.startswith("--region=") or low.startswith("--location="):
            zone_or_region = tok.split("=", 1)[1]
        elif low in {"--region", "--location", "-r", "-l"} and i + 1 < len(tokens):
            zone_or_region = tokens[i + 1]

    parts: list[str] = []
    if action and resource_type:
        parts.append(f"The command will {action} {resource_type}")
    if resource_name and not resource_name.startswith("--"):
        parts.append(f"named '{resource_name}'")
    if zone_or_region:
        parts.append(f"in {zone_or_region}")

    summary_core = " ".join(parts) if parts else cmd
    return f"{summary_core}.\n\n"

logger = logging.getLogger(__name__)


def _extract_serial_port_pagination_hint(stderr_text: str) -> Optional[str]:
    """
    Detect gcloud serial-port pagination hints so we can surface them as context
    instead of treating them as errors.
    """
    if not stderr_text:
        return None
    
    for line in stderr_text.splitlines():
        lowered = line.lower()
        if "get-serial-port-output" in lowered and "--start=" in lowered:
            return line.strip()
    return None


def _sanitize_no_truncate(output: str) -> str:
    """
    Sanitize output for WebSocket transmission without truncating.
    Specifically used for serial-port logs where full output is desired.
    """
    if not output:
        return output
    try:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        cleaned = ansi_escape.sub('', output)
        cleaned = cleaned.replace('\x00', '')
        cleaned = cleaned.encode('utf-8', errors='replace').decode('utf-8')
        return cleaned
    except Exception:
        return "[content sanitized due to encoding error]"


def check_cli_availability(cli_tool: str) -> bool:
    """Check if a CLI tool is available in the system PATH."""
    try:
        # Use 'which' on Unix-like systems or 'where' on Windows to check if command exists
        check_cmd = ["which", cli_tool] if os.name != 'nt' else ["where", cli_tool]
        result = terminal_run(
            check_cmd,
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        # Fallback: try to run the command with --version
        try:
            result = terminal_run(
                [cli_tool, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

# OLD GLOBAL AZURE FUNCTION REMOVED - Use setup_azure_environment_isolated() instead


def setup_azure_environment_isolated(user_id: str, subscription_id: str | None = None):
    """Set up Azure environment with isolated credentials - NO global state modification."""
    try:
        logger.info("Setting up isolated Azure environment...")
        
        current_mode = get_mode_from_context()
        azure_creds = generate_azure_access_token(user_id, subscription_id, mode=current_mode)
        access_token = azure_creds["access_token"]
        subscription_id = azure_creds["subscription_id"]
        tenant_id = azure_creds["tenant_id"]
        client_id = azure_creds.get("client_id")
        client_secret = azure_creds.get("client_secret")

        if not all([tenant_id, client_id, client_secret]):
            raise ValueError("Incomplete Azure credentials for CLI authentication")
        
        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/home/appuser",  # Use terminal pod's writable home directory
            "USER": os.environ.get("USER", ""),
            "AZURE_CLIENT_ID": str(client_id),
            "AZURE_CLIENT_SECRET": str(client_secret),
            "AZURE_TENANT_ID": str(tenant_id),
            "AZURE_CONFIG_DIR": "/home/appuser/.azure",  # Ensure Azure CLI uses correct config directory
        }
        
        # Store auth command for chaining with user commands (NEVER log the secret!)
        auth_command = f"az login --service-principal --username {client_id} --password {client_secret} --tenant {tenant_id} --output none"

        logger.info(f"Azure isolated environment configured for subscription: {subscription_id} (auth_command built with --password [REDACTED])")
        
        return True, subscription_id, "service_principal", isolated_env, auth_command
        
    except Exception as e:
        logger.error(f"Failed to setup Azure environment: {e}")
        return False, None, None, None, None


def _get_region_for_account(user_id: str, account_id: str) -> Optional[str]:
    """Look up the region for a specific AWS account connection."""
    from utils.db.connection_utils import get_all_user_aws_connections
    for conn in get_all_user_aws_connections(user_id):
        if conn.get("account_id") == account_id:
            return conn.get("region") or "us-east-1"
    return None


def setup_aws_environment_isolated(user_id: str, selected_region: str | None = None, target_account_id: str | None = None):
    """Set up AWS environment with isolated credentials - NO global state modification."""
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up isolated AWS environment...")
        
        # ------------------------------------------------------------------
        # NEW WORKSPACE-ONLY FLOW – no static credential lookup.
        # ------------------------------------------------------------------

        try:
            # Single source of truth: read from user_connections
            from utils.db.connection_utils import get_user_aws_connection
            from utils.workspace.workspace_utils import get_or_create_workspace
            from utils.aws.aws_sts_client import assume_workspace_role
            from utils.aws.aws_session_policies import get_read_only_session_policy

            if target_account_id:
                from utils.db.connection_utils import get_all_user_aws_connections
                aws_conn = None
                for c in get_all_user_aws_connections(user_id):
                    if c.get("account_id") == target_account_id:
                        aws_conn = c
                        break
                if not aws_conn:
                    logger.error("No AWS connection found for account %s", target_account_id)
                    return False, None, None, None
            else:
                aws_conn = get_user_aws_connection(user_id)
            if not aws_conn or not aws_conn.get('role_arn'):
                logger.error("User %s does not have an active AWS connection", user_id)
                return False, None, None, None

            conn_region = aws_conn.get("region")
            if conn_region and not selected_region:
                selected_region = conn_region

            # Get external_id from workspace (needed for STS AssumeRole)
            ws = get_or_create_workspace(user_id, "default")
            external_id = ws.get("aws_external_id")
            if not external_id:
                logger.error("Workspace %s for user %s missing aws_external_id", ws["id"], user_id)
                return False, None, None, None

            current_mode = get_mode_from_context()
            role_arn = aws_conn.get("role_arn")

            # Apply session policy for read-only mode
            session_policy = None
            if ModeAccessController.is_read_only_mode(current_mode):
                # First check if user has a dedicated read-only role configured
                read_only_role = aws_conn.get("read_only_role_arn")
                if read_only_role:
                    # Use dedicated read-only role if available
                    role_arn = read_only_role
                    logger.info("Using AWS read-only role for user %s", user_id)
                else:
                    # Apply restrictive session policy to make the role read-only
                    # NOTE: Session policies work as an intersection with base role permissions.
                    # If the base role lacks read permissions, this may fail at runtime.
                    # For reliable read-only mode, users should provide a dedicated read-only role.
                    session_policy = get_read_only_session_policy()
                    logger.warning(
                        "Read-only mode enabled for user %s but no read_only_role_arn provided. "
                        "Using session policy fallback. This may fail if the base role lacks read permissions. "
                        "Consider providing a dedicated read-only role for better reliability.",
                        user_id
                    )

            sts_creds = assume_workspace_role(
                role_arn=role_arn,
                external_id=external_id,
                workspace_id=ws["id"],
                region=selected_region or "us-east-1",
                session_policy=session_policy
            )

            aws_credentials = {
                "aws_access_key_id": sts_creds["accessKeyId"],
                "aws_secret_access_key": sts_creds["secretAccessKey"],
                "aws_session_token": sts_creds["sessionToken"],
                # Always include region list for downstream selection
                "aws_regions": [selected_region or "us-east-1"],
            }

            logger.info("Assumed workspace role for %s, obtained temporary credentials (expires %s)", ws["id"], sts_creds["expiration"])

        except Exception as e:
            logger.error("Failed to obtain AWS workspace credentials: %s", e)
            return False, None, None, None
        
        access_key_id = aws_credentials['aws_access_key_id']
        secret_access_key = aws_credentials['aws_secret_access_key']
        session_token = aws_credentials.get('aws_session_token')
        
        # Get regions - use selected_region if provided, otherwise use first from stored regions
        regions = aws_credentials.get('aws_regions', ['us-east-1'])
        if isinstance(regions, list) and regions:
            if selected_region and selected_region in regions:
                region = selected_region
            else:
                region = regions[0]
        else:
            region = selected_region or 'us-east-1'
        
        logger.info(f"Using AWS region: {region}")
        
        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/home/appuser",  # Use terminal pod's writable home directory
            "USER": os.environ.get("USER", ""),
            "AWS_ACCESS_KEY_ID": str(access_key_id),
            "AWS_SECRET_ACCESS_KEY": str(secret_access_key),
        }
        if session_token:
            isolated_env["AWS_SESSION_TOKEN"] = str(session_token)
            # Legacy var for some AWS CLI versions
            isolated_env["AWS_SECURITY_TOKEN"] = str(session_token)

        # Ensure region is available to CLI/SDK
        isolated_env["AWS_DEFAULT_REGION"] = region
        
        # Validate credentials by making a test call to AWS STS
        try:
            # Temporarily set environment to prevent boto3 from looking for profiles
            # We'll use a context manager approach for thread safety
            import contextlib
            
            @contextlib.contextmanager
            def temporary_aws_env_override():
                """Context manager to temporarily override AWS env vars for boto3."""
                saved_vars = {}
                vars_to_clear = ["AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", 
                                "AWS_SHARED_CREDENTIALS_FILE", "AWS_CREDENTIAL_FILE"]
                
                # Save and clear problematic environment variables
                for var in vars_to_clear:
                    if var in os.environ:
                        saved_vars[var] = os.environ[var]
                        del os.environ[var]
                
                # Set dummy AWS_CONFIG_FILE to prevent boto3 from searching for config
                os.environ["AWS_CONFIG_FILE"] = "/dev/null"
                os.environ["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
                
                try:
                    yield
                finally:
                    # Restore original environment
                    os.environ.pop("AWS_CONFIG_FILE", None)
                    os.environ.pop("AWS_SHARED_CREDENTIALS_FILE", None)
                    for var, value in saved_vars.items():
                        os.environ[var] = value
            
            # Use the context manager to ensure clean environment for boto3
            with temporary_aws_env_override():
                import boto3
                from botocore.config import Config
                
                sts_start = time.perf_counter()
                
                # Create a config to avoid profile issues
                config = Config(
                    region_name=region,
                    signature_version='v4',
                    retries={'max_attempts': 3}
                )
                
                # Use explicit credentials with boto3.client
                sts_kwargs = {
                    'service_name': 'sts',
                    'aws_access_key_id': access_key_id,
                    'aws_secret_access_key': secret_access_key,
                    'region_name': region,
                    'config': config,
                    'use_ssl': True,
                }
                if session_token:
                    sts_kwargs['aws_session_token'] = session_token

                sts = boto3.client(**sts_kwargs)
                
                identity = sts.get_caller_identity()
                logger.info(f"TIME: AWS STS validation took {time.perf_counter() - sts_start:.2f}s")
                
                account_id = identity['Account']
                user_arn = identity.get('Arn', 'Unknown')
                logger.info(f"Successfully validated AWS credentials for account: {account_id}")
                logger.info(f"User ARN: {user_arn}")

                # Stash account ID in isolated env for downstream display without extra calls
                try:
                    isolated_env["AURORA_AWS_ACCOUNT_ID"] = str(account_id)
                except Exception:
                    pass

                # Also attempt to resolve a friendly account alias (optional)
                try:
                    iam = boto3.client(
                        'iam',
                        aws_access_key_id=access_key_id,
                        aws_secret_access_key=secret_access_key,
                        aws_session_token=session_token if session_token else None,
                        config=config,
                        use_ssl=True,
                    )
                    alias_resp = iam.list_account_aliases(MaxItems=1)
                    aliases = alias_resp.get('AccountAliases', [])
                    if isinstance(aliases, list) and aliases:
                        isolated_env["AURORA_AWS_ACCOUNT_ALIAS"] = str(aliases[0])
                except Exception as alias_err:
                    logger.info(f"Could not resolve AWS account alias: {alias_err}")
                    
        except Exception as e:
            logger.error(f"AWS credentials validation failed: {e}")
            return False, None, None, None
        
        logger.info(f"AWS isolated environment configured for region: {region}")
        logger.info(f"TIME: setup_aws_environment_isolated completed in {time.perf_counter() - fn_start:.2f}s")
        
        return True, region, "access_key", isolated_env
        
    except Exception as e:
        logger.error(f"Failed to setup AWS environment: {e}")
        return False, None, None, None


def setup_aws_environments_all_accounts(user_id: str):
    """Assume roles across all connected AWS accounts and return credential dicts.

    Returns a list of dicts, each containing:
        - account_id
        - region
        - credentials (accessKeyId, secretAccessKey, sessionToken)
        - isolated_env (ready-to-use env dict for subprocess calls)

    Failed accounts are logged and skipped; the caller receives only the
    accounts that were successfully assumed.
    """
    from utils.db.connection_utils import get_all_user_aws_connections
    from utils.workspace.workspace_utils import get_or_create_workspace
    from utils.aws.aws_sts_client import assume_workspace_role
    from utils.aws.aws_session_policies import get_read_only_session_policy

    ws = get_or_create_workspace(user_id, "default")
    external_id = ws.get("aws_external_id")
    if not external_id:
        logger.error("Workspace %s for user %s missing aws_external_id", ws["id"], user_id)
        return []

    connections = get_all_user_aws_connections(user_id)
    if not connections:
        logger.warning("No active AWS connections for user %s", user_id)
        return []

    current_mode = get_mode_from_context()
    default_session_policy = None
    if ModeAccessController.is_read_only_mode(current_mode):
        default_session_policy = get_read_only_session_policy()

    account_envs = []
    for conn in connections:
        role_arn = conn.get("role_arn")
        account_id = conn.get("account_id")
        region = conn.get("region") or "us-east-1"
        session_policy = default_session_policy

        if not role_arn:
            logger.warning("Skipping account %s – no role_arn", account_id)
            continue

        if ModeAccessController.is_read_only_mode(current_mode):
            ro_arn = conn.get("read_only_role_arn")
            if ro_arn:
                role_arn = ro_arn
                session_policy = None

        try:
            creds = assume_workspace_role(
                role_arn=role_arn,
                external_id=external_id,
                workspace_id=ws["id"],
                region=region,
                session_policy=session_policy,
            )
        except Exception as e:
            logger.error("Failed to assume role for account %s: %s", account_id, e)
            continue

        isolated_env = {
            "AWS_ACCESS_KEY_ID": creds["accessKeyId"],
            "AWS_SECRET_ACCESS_KEY": creds["secretAccessKey"],
            "AWS_SESSION_TOKEN": creds["sessionToken"],
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
            "PATH": os.environ.get("PATH", ""),
        }

        account_envs.append({
            "account_id": account_id,
            "region": region,
            "credentials": creds,
            "isolated_env": isolated_env,
        })

    logger.info(
        "Assumed roles for %d / %d accounts for user %s",
        len(account_envs), len(connections), user_id,
    )
    return account_envs


# OLD GLOBAL GCP FUNCTION REMOVED - Use setup_gcp_environment_isolated() instead  
def setup_gcp_impersonation_DEPRECATED(user_id: str, selected_project_id: str | None = None, provider_preference: str | None = None):
    """Set up GCP authentication using OAuth impersonation."""
    try:
        fn_start = time.perf_counter()  # Start timing for entire impersonation setup
        logger.info("Attempting impersonated access...")
        token_start = time.perf_counter()
        current_mode = get_mode_from_context()
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

        # Method 1: Use environment variables that Cloud SDK & gsutil respect
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = access_token
        os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN"] = access_token  # gcloud/gsutil pick this up
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

        # Ensure all Cloud SDK commands (including gsutil) impersonate the SA.
        # Setting this env var is honoured by gcloud, gsutil, bq, etc.
        os.environ["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email  # new CLIs
        os.environ["CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT"] = sa_email       # legacy CLIs
        
        # Method 2: Also try to configure gcloud settings
        try:
            # Set the default project
            project_cmd = ["gcloud", "config", "set", "project", project_id]
            config_start = time.perf_counter()
            proj_result = terminal_run(
                project_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            logger.info(f"TIME: gcloud config set project took {time.perf_counter() - config_start:.2f}s")
            
            if proj_result.returncode == 0:
                logger.info(f"Successfully set default project: {project_id}")
            else:
                logger.warning(f"Failed to set default project: {proj_result.stderr}")

            # Configure impersonation property so subsequent CLI calls inherit it.
            imp_cmd = ["gcloud", "config", "set", "auth/impersonate_service_account", sa_email]
            imp_start = time.perf_counter()
            imp_result = terminal_run(
                imp_cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            logger.info(f"TIME: gcloud config set impersonate_sa took {time.perf_counter() - imp_start:.2f}s")
            if imp_result.returncode == 0:
                logger.info(f"Configured gcloud to impersonate {sa_email}")
            else:
                logger.warning(f"Failed to configure SA impersonation: {imp_result.stderr}")
            
            # Verify the configuration
            try:
                auth_start = time.perf_counter()
                auth_list = terminal_run(
                    ['gcloud', 'auth', 'list'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                logger.info(f"TIME: gcloud auth list took {time.perf_counter() - auth_start:.2f}s")
                logger.info(f"Current auth configuration: {auth_list.stdout}")
            except Exception as e:
                logger.warning(f"Failed to verify auth configuration: {e}")
                
        except Exception as e:
            logger.warning(f"Failed to configure gcloud settings: {e}")
        
        logger.info(f"Successfully set up impersonated access for project: {project_id}")
        logger.info(f"TIME: setup_gcp_impersonation completed in {time.perf_counter() - fn_start:.2f}s")
        return True, project_id, "impersonated"

    except Exception as e:
        logger.error(f"Failed to generate SA access token: {e}")
        return False, None, None


def setup_gcp_environment_isolated(user_id: str, selected_project_id: str | None = None, provider_preference: str | None = None):
    """Set up GCP environment with isolated credentials - NO global state modification."""
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up isolated GCP environment...")

        token_start = time.perf_counter()
        current_mode = get_mode_from_context()
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

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/home/appuser",  # Use terminal pod's writable home directory
            "USER": os.environ.get("USER", ""),
            "GOOGLE_OAUTH_ACCESS_TOKEN": access_token,
            "CLOUDSDK_AUTH_ACCESS_TOKEN": access_token,
            "GOOGLE_CLOUD_PROJECT": project_id,
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": sa_email,
            "CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT": sa_email,
            "CLOUDSDK_CONFIG": "/tmp/.gcloud",
        }
        
        logger.info(f"GCP isolated environment configured for project: {project_id}")
        logger.info(f"TIME: setup_gcp_environment_isolated completed in {time.perf_counter() - fn_start:.2f}s")
        
        return True, project_id, "impersonated", isolated_env

    except Exception as e:
        logger.error(f"Failed to generate SA access token: {e}")
        return False, None, None, None


def setup_ovh_environment_isolated(user_id: str, selected_project_id: str | None = None):
    """Set up OVH environment with isolated credentials - NO global state modification."""
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up isolated OVH environment...")

        # Get OVH token data from database
        from utils.secrets.secret_ref_utils import get_user_token_data
        token_data = get_user_token_data(user_id, 'ovh')

        if not token_data:
            logger.error("No OVH credentials found for user")
            return False, None, None, None

        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint', 'ovh-us')
        expires_at = token_data.get('expires_at', 0)
        refresh_token = token_data.get('refresh_token')
        project_id = selected_project_id or token_data.get('projectId')

        # Check if token is expired (with 5-minute buffer)
        current_time = int(time.time())
        if expires_at and current_time >= (expires_at - 300):
            logger.info("OVH access token expired or expiring soon, attempting refresh...")
            if refresh_token:
                # Attempt to refresh the token
                try:
                    from connectors.ovh_connector.oauth2_config import get_oauth2_config
                    oauth2_config = get_oauth2_config()
                    client_config = oauth2_config.get(endpoint, {})
                    
                    # OVH token refresh endpoint
                    token_endpoints = {
                        'ovh-eu': 'https://www.ovh.com/auth/oauth2/token',
                        'ovh-us': 'https://us.ovhcloud.com/auth/oauth2/token',
                        'ovh-ca': 'https://ca.ovhcloud.com/auth/oauth2/token',
                    }
                    token_url = token_endpoints.get(endpoint, token_endpoints['ovh-us'])
                    
                    refresh_response = requests.post(
                        token_url,
                        data={
                            'grant_type': 'refresh_token',
                            'refresh_token': refresh_token,
                            'client_id': client_config.get('client_id'),
                            'client_secret': client_config.get('client_secret'),
                        },
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                        timeout=30
                    )
                    
                    if refresh_response.ok:
                        new_token_data = refresh_response.json()
                        access_token = new_token_data.get('access_token')
                        new_expires_in = new_token_data.get('expires_in', 3600)
                        new_refresh_token = new_token_data.get('refresh_token', refresh_token)
                        
                        # Update stored tokens
                        from utils.auth.stateless_auth import store_tokens_in_db
                        updated_storage = {
                            "endpoint": endpoint,
                            "client_id": client_config.get('client_id'),
                            "access_token": access_token,
                            "token_type": new_token_data.get('token_type', 'Bearer'),
                            "expires_at": int(time.time()) + new_expires_in,
                            "refresh_token": new_refresh_token,
                            "auth_method": "authorization_code",
                        }
                        if project_id:
                            updated_storage["projectId"] = project_id
                        store_tokens_in_db(user_id, updated_storage, 'ovh')
                        logger.info("Successfully refreshed OVH access token")
                    else:
                        logger.error(f"Failed to refresh OVH token: {refresh_response.status_code}")
                        return False, None, None, None
                except Exception as refresh_err:
                    logger.error(f"Error refreshing OVH token: {refresh_err}")
                    return False, None, None, None
            else:
                logger.error("OVH token expired and no refresh token available")
                return False, None, None, None

        if not access_token:
            logger.error("No OVH access token available")
            return False, None, None, None

        # Map endpoint to OVH API endpoint format
        endpoint_map = {
            'ovh-eu': 'ovh-eu',
            'ovh-us': 'ovh-us',
            'ovh-ca': 'ovh-ca',
        }
        ovh_endpoint = endpoint_map.get(endpoint, 'ovh-us')

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/home/appuser",
            "USER": os.environ.get("USER", ""),
            "OVH_ACCESS_TOKEN": access_token,
            "OVH_ENDPOINT": ovh_endpoint,
        }
        
        # Add default project if available
        if project_id:
            isolated_env["OVH_CLOUD_PROJECT_SERVICE"] = project_id

        logger.info(f"OVH isolated environment configured for endpoint: {ovh_endpoint}")
        logger.info(f"TIME: setup_ovh_environment_isolated completed in {time.perf_counter() - fn_start:.2f}s")

        return True, project_id, "oauth2_access_token", isolated_env

    except Exception as e:
        logger.error(f"Failed to setup OVH environment: {e}")
        return False, None, None, None


def setup_scaleway_environment_isolated(user_id: str, selected_project_id: str | None = None):
    """Set up Scaleway environment with isolated credentials - NO global state modification."""
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up isolated Scaleway environment...")

        # Get Scaleway token data from database
        from utils.secrets.secret_ref_utils import get_user_token_data
        token_data = get_user_token_data(user_id, 'scaleway')

        if not token_data:
            logger.error("No Scaleway credentials found for user")
            return False, None, None, None

        access_key = token_data.get('access_key')
        secret_key = token_data.get('secret_key')
        organization_id = token_data.get('organization_id')
        # Token data uses 'default_project_id' key, not 'project_id'
        project_id = selected_project_id or token_data.get('default_project_id')
        # Region/zone: use stored preference if available, otherwise let CLI use its defaults
        # Scaleway regions: fr-par, nl-ams, pl-waw (each has zones like fr-par-1, nl-ams-1, etc.)
        default_region = token_data.get('default_region')
        default_zone = token_data.get('default_zone')

        if not access_key or not secret_key:
            logger.error("Missing Scaleway access_key or secret_key")
            return False, None, None, None

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        # Scaleway CLI uses these environment variables:
        # SCW_ACCESS_KEY, SCW_SECRET_KEY, SCW_DEFAULT_ORGANIZATION_ID, SCW_DEFAULT_PROJECT_ID
        # SCW_DEFAULT_REGION, SCW_DEFAULT_ZONE
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/home/appuser",
            "USER": os.environ.get("USER", ""),
            "SCW_ACCESS_KEY": access_key,
            "SCW_SECRET_KEY": secret_key,
        }
        
        # Add organization ID if available
        if organization_id:
            isolated_env["SCW_DEFAULT_ORGANIZATION_ID"] = organization_id
        
        # Add default project if available
        if project_id:
            isolated_env["SCW_DEFAULT_PROJECT_ID"] = project_id
        
        # Add region/zone if configured (otherwise CLI uses its own defaults)
        if default_region:
            isolated_env["SCW_DEFAULT_REGION"] = default_region
        if default_zone:
            isolated_env["SCW_DEFAULT_ZONE"] = default_zone

        logger.info(f"Scaleway isolated environment configured (region: {default_region or 'CLI default'})")
        logger.info(f"TIME: setup_scaleway_environment_isolated completed in {time.perf_counter() - fn_start:.2f}s")

        return True, project_id, "api_key", isolated_env

    except Exception as e:
        logger.error(f"Failed to setup Scaleway environment: {e}")
        return False, None, None, None


def setup_tailscale_environment_isolated(user_id: str, selected_tailnet: str | None = None):
    """Set up Tailscale environment with isolated credentials - NO global state modification.

    Unlike other providers, Tailscale uses REST API instead of CLI.
    Returns the access token and tailnet for API calls.
    """
    try:
        fn_start = time.perf_counter()
        logger.info("Setting up isolated Tailscale environment...")

        # Get Tailscale token data from database
        from utils.secrets.secret_ref_utils import get_user_token_data
        stored_data = get_user_token_data(user_id, 'tailscale')

        if not stored_data:
            logger.error("No Tailscale credentials found for user")
            return False, None, None, None

        # Extract credentials from stored data
        client_id = stored_data.get('client_id')
        client_secret = stored_data.get('client_secret')
        nested_token_data = stored_data.get('token_data', {})
        tailnet = selected_tailnet or stored_data.get('tailnet') or '-'
        tailnet_name = stored_data.get('tailnet_name', tailnet)

        if not client_id or not client_secret:
            logger.error("Missing Tailscale client credentials")
            return False, None, None, None

        # Get valid access token (handles refresh if needed)
        from connectors.tailscale_connector.auth import get_valid_access_token
        success, access_token, error = get_valid_access_token(
            client_id, client_secret, nested_token_data
        )

        if not success or not access_token:
            logger.error(f"Failed to get valid Tailscale access token: {error}")
            return False, None, None, None

        # For Tailscale, we don't use environment variables for CLI
        # Instead, we return the token for direct API usage
        # The isolated_env here is used to pass token info to the command executor
        isolated_env = {
            "TAILSCALE_ACCESS_TOKEN": access_token,
            "TAILSCALE_TAILNET": tailnet,
            "TAILSCALE_TAILNET_NAME": tailnet_name,
        }

        logger.info(f"Tailscale isolated environment configured (tailnet: {tailnet_name})")
        logger.info(f"TIME: setup_tailscale_environment_isolated completed in {time.perf_counter() - fn_start:.2f}s")

        return True, tailnet, "oauth", isolated_env

    except Exception as e:
        logger.error(f"Failed to setup Tailscale environment: {e}")
        return False, None, None, None


def execute_tailscale_command(command: str, isolated_env: dict) -> dict:
    """Execute a Tailscale command by translating it to REST API calls.

    Tailscale doesn't have a CLI in the same sense as other providers.
    This function translates text commands to TailscaleClient API calls.

    Supported commands:
    - device list / devices
    - device get <device_id>
    - device authorize <device_id>
    - device delete <device_id>
    - device tags <device_id> <tag1> <tag2> ...
    - auth-key list / keys
    - auth-key create [--reusable] [--ephemeral] [--tags tag1,tag2]
    - auth-key delete <key_id>
    - acl get / acl show
    - dns nameservers
    - dns searchpaths
    - dns preferences
    - routes list / routes
    - status
    """
    from connectors.tailscale_connector.api_client import TailscaleClient

    access_token = isolated_env.get("TAILSCALE_ACCESS_TOKEN")
    tailnet = isolated_env.get("TAILSCALE_TAILNET", "-")

    if not access_token:
        return {
            "success": False,
            "error": "No Tailscale access token available",
            "return_code": 1
        }

    client = TailscaleClient(access_token)

    # Parse the command
    # Remove 'tailscale' prefix if present
    cmd = command.strip()
    if cmd.lower().startswith('tailscale '):
        cmd = cmd[10:].strip()

    parts = cmd.split()
    if not parts:
        return {
            "success": False,
            "error": "Empty command",
            "return_code": 1
        }

    action = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []

    try:
        # Device commands
        if action in ['device', 'devices']:
            if not args or args[0].lower() == 'list':
                success, devices, error = client.list_devices(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(devices, indent=2),
                        "chat_output": json.dumps(devices, indent=2),
                        "return_code": 0,
                        "device_count": len(devices)
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'get' and len(args) > 1:
                device_id = args[1]
                success, device, error = client.get_device(device_id)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(device, indent=2),
                        "chat_output": json.dumps(device, indent=2),
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'authorize' and len(args) > 1:
                device_id = args[1]
                success, error = client.authorize_device(device_id)
                if success:
                    return {
                        "success": True,
                        "output": f"Device {device_id} authorized successfully",
                        "chat_output": f"Device {device_id} authorized successfully",
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'delete' and len(args) > 1:
                device_id = args[1]
                success, error = client.delete_device(device_id)
                if success:
                    return {
                        "success": True,
                        "output": f"Device {device_id} deleted successfully",
                        "chat_output": f"Device {device_id} deleted successfully",
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'tags' and len(args) > 1:
                device_id = args[1]
                tags = args[2:] if len(args) > 2 else []
                success, error = client.set_device_tags(device_id, tags)
                if success:
                    return {
                        "success": True,
                        "output": f"Tags set on device {device_id}: {tags}",
                        "chat_output": f"Tags set on device {device_id}: {tags}",
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

        # Auth key commands
        elif action in ['auth-key', 'key', 'keys', 'authkey']:
            if not args or args[0].lower() == 'list':
                success, keys, error = client.list_auth_keys(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(keys, indent=2),
                        "chat_output": json.dumps(keys, indent=2),
                        "return_code": 0,
                        "key_count": len(keys)
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'create':
                # Parse optional flags
                reusable = '--reusable' in args
                ephemeral = '--ephemeral' in args
                tags = []
                for i, arg in enumerate(args):
                    if arg == '--tags' and i + 1 < len(args):
                        tags = args[i + 1].split(',')
                        break

                success, key_data, error = client.create_auth_key(
                    tailnet=tailnet,
                    reusable=reusable,
                    ephemeral=ephemeral,
                    tags=tags if tags else None
                )
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(key_data, indent=2),
                        "chat_output": f"Auth key created: {key_data.get('key', 'See output for key')}",
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'delete' and len(args) > 1:
                key_id = args[1]
                success, error = client.delete_auth_key(tailnet, key_id)
                if success:
                    return {
                        "success": True,
                        "output": f"Auth key {key_id} deleted successfully",
                        "chat_output": f"Auth key {key_id} deleted successfully",
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

        # ACL commands
        elif action == 'acl':
            if not args or args[0].lower() in ['get', 'show']:
                success, acl, error = client.get_acl(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(acl, indent=2),
                        "chat_output": json.dumps(acl, indent=2),
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

        # DNS commands
        elif action == 'dns':
            if not args or args[0].lower() == 'nameservers':
                success, dns, error = client.get_dns_nameservers(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(dns, indent=2),
                        "chat_output": json.dumps(dns, indent=2),
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'searchpaths':
                success, paths, error = client.get_dns_searchpaths(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(paths, indent=2),
                        "chat_output": json.dumps(paths, indent=2),
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

            elif args[0].lower() == 'preferences':
                success, prefs, error = client.get_dns_preferences(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(prefs, indent=2),
                        "chat_output": json.dumps(prefs, indent=2),
                        "return_code": 0
                    }
                return {"success": False, "error": error, "return_code": 1}

        # Routes commands
        elif action in ['routes', 'route']:
            if not args or args[0].lower() == 'list':
                success, routes, error = client.get_routes(tailnet)
                if success:
                    return {
                        "success": True,
                        "output": json.dumps(routes, indent=2),
                        "chat_output": json.dumps(routes, indent=2),
                        "return_code": 0,
                        "route_count": len(routes)
                    }
                return {"success": False, "error": error, "return_code": 1}

        # Status command - list devices as a quick status check
        elif action == 'status':
            success, devices, error = client.list_devices(tailnet)
            if success:
                online_count = sum(1 for d in devices if d.get('lastSeen'))
                return {
                    "success": True,
                    "output": json.dumps({
                        "tailnet": tailnet,
                        "device_count": len(devices),
                        "devices": devices
                    }, indent=2),
                    "chat_output": f"Tailnet: {tailnet}\nDevices: {len(devices)} total",
                    "return_code": 0
                }
            return {"success": False, "error": error, "return_code": 1}

        # Settings command
        elif action == 'settings':
            success, settings, error = client.get_tailnet_settings(tailnet)
            if success:
                return {
                    "success": True,
                    "output": json.dumps(settings, indent=2),
                    "chat_output": json.dumps(settings, indent=2),
                    "return_code": 0
                }
            return {"success": False, "error": error, "return_code": 1}

        # Unknown command
        return {
            "success": False,
            "error": f"Unknown Tailscale command: {action}. Supported commands: device list/get/authorize/delete/tags, auth-key list/create/delete, acl get, dns nameservers/searchpaths/preferences, routes list, status, settings",
            "return_code": 1
        }

    except Exception as e:
        logger.error(f"Error executing Tailscale command: {e}")
        return {
            "success": False,
            "error": f"Failed to execute Tailscale command: {str(e)}",
            "return_code": 1
        }


# OLD GLOBAL AWS FUNCTION REMOVED - Use setup_aws_environment_isolated() instead

def setup_kubeconfig_for_eks(cluster_name: str, region: str) -> bool:
    """Set up kubeconfig for EKS cluster using AWS CLI."""
    try:
        logger.info(f"Setting up kubeconfig for EKS cluster: {cluster_name} in {region}")
        
        # Update kubeconfig for the EKS cluster
        kubeconfig_cmd = f"aws eks update-kubeconfig --name {cluster_name} --region {region}"
        result = terminal_run(
            shlex.split(kubeconfig_cmd),
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ.copy()
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully updated kubeconfig for cluster {cluster_name}")
            return True
        else:
            logger.error(f"Failed to update kubeconfig: {result.stderr}")
            return False
            
    except Exception as e:
        logger.error(f"Error setting up kubeconfig: {e}")
        return False

def deploy_helm_chart(chart_path: str, release_name: str, namespace: str = "default", values_file: str = None, kube_context: str = None) -> str:
    """Deploy a Helm chart using the cloud execution tool."""
    try:
        logger.info(f"Deploying Helm chart: {chart_path} as {release_name} in namespace {namespace}")
        
        # Build the helm upgrade --install command
        helm_cmd = f"helm upgrade --install {release_name} {chart_path} --namespace {namespace}"
        
        # Add values file if specified
        if values_file:
            helm_cmd += f" --values {values_file}"
        
        # Add kube context if specified
        if kube_context:
            helm_cmd += f" --kube-context {kube_context}"
        
        # Add additional flags for better output
        helm_cmd += " --wait --timeout 10m"
        
        logger.info(f"Executing Helm command: {helm_cmd}")
        
        # Execute the helm command
        result = terminal_run(
            shlex.split(helm_cmd),
            capture_output=True,
            text=True,
            timeout=600,  # 10 minutes timeout for Helm operations
            env=os.environ.copy()
        )
        
        if result.returncode == 0:
            logger.info(f"Helm deployment successful for {release_name}")
            return json.dumps({
                "success": True,
                "message": f"Helm chart {release_name} deployed successfully",
                "output": result.stdout,
                "release_name": release_name,
                "namespace": namespace
            })
        else:
            logger.error(f"Helm deployment failed: {result.stderr}")
            return json.dumps({
                "success": False,
                "error": f"Helm deployment failed: {result.stderr}",
                "output": result.stdout,
                "release_name": release_name,
                "namespace": namespace
            })
            
    except Exception as e:
        logger.error(f"Error deploying Helm chart: {e}")
        return json.dumps({
            "success": False,
            "error": f"Helm deployment error: {str(e)}",
            "release_name": release_name,
            "namespace": namespace
        })

def extract_and_deploy_helm_chart(zip_path: str, cluster_name: str, region: str, release_name: str = None) -> str:
    """Extract Helm chart from zip and deploy it to EKS cluster."""
    try:
        import zipfile
        import tempfile
        import os
        
        logger.info(f"Extracting and deploying Helm chart from {zip_path}")
        
        # Create temporary directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            # Extract the zip file
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Look for deploy directory or Chart.yaml
            chart_path = None
            values_file = None
            
            # Check for deploy directory first
            deploy_dir = os.path.join(temp_dir, "deploy")
            if os.path.exists(deploy_dir) and os.path.isdir(deploy_dir):
                chart_path = deploy_dir
                values_file = os.path.join(deploy_dir, "values.yaml")
                if not os.path.exists(values_file):
                    values_file = None
            else:
                # Look for Chart.yaml in root or subdirectories
                for root, dirs, files in os.walk(temp_dir):
                    if "Chart.yaml" in files:
                        chart_path = root
                        # Look for values.yaml in the same directory
                        potential_values = os.path.join(root, "values.yaml")
                        if os.path.exists(potential_values):
                            values_file = potential_values
                        break
            
            if not chart_path:
                return json.dumps({
                    "success": False,
                    "error": "No Helm chart found in the zip file. Expected 'deploy' directory or 'Chart.yaml' file."
                })
            
            # Set up kubeconfig for the EKS cluster
            if not setup_kubeconfig_for_eks(cluster_name, region):
                return json.dumps({
                    "success": False,
                    "error": f"Failed to set up kubeconfig for EKS cluster {cluster_name}"
                })
            
            # Use provided release name or default to chart name
            if not release_name:
                # Try to extract chart name from Chart.yaml
                chart_yaml_path = os.path.join(chart_path, "Chart.yaml")
                if os.path.exists(chart_yaml_path):
                    try:
                        with open(chart_yaml_path, 'r') as f:
                            for line in f:
                                if line.startswith('name:'):
                                    release_name = line.split(':', 1)[1].strip()
                                    break
                    except Exception:
                        pass
                
                if not release_name:
                    release_name = "aurora-app"
            
            # Deploy the Helm chart
            return deploy_helm_chart(chart_path, release_name, "default", values_file)
            
    except Exception as e:
        logger.error(f"Error extracting and deploying Helm chart: {e}")
        return json.dumps({
            "success": False,
            "error": f"Failed to extract and deploy Helm chart: {str(e)}"
        })

def is_read_only_command(command: str) -> bool:
    """Check if a cloud command is read-only (list, describe, get, etc.)."""
    read_only_verbs = ['list', 'describe', 'get', 'show', 'config', 'version', 'info', 'status', 'read', 'view', 'help', 'logs', 'top']

    # Check if any read-only verb is in the command
    for verb in read_only_verbs:
        if verb in command.lower():
            return True

    # Additional check for common read-only patterns
    read_only_patterns = [
        '--filter', '--output=json', '--query',
        'status:running', '--dry-run', 'explain', 'diff',
        'logging read', 'logging list', 'logs read', 'logs list',
        # Tailscale-specific read operations
        'dns nameservers', 'dns searchpaths', 'dns preferences',
        'routes', 'settings', 'acl get', 'acl show', 'devices', 'keys'
    ]
    if any(pattern in command.lower() for pattern in read_only_patterns):
        return True

    # Additional heuristics/patterns – mostly for legacy support.
    lowered = command.lower()
    if any(p in lowered for p in ["--filter", "status:running"]):
        return True

    # Default to **not** read-only to err on the side of caution.
    return False


def get_command_timeout(command: str, user_timeout: int = None) -> int:
    """Determine timeout for a command. Use user_timeout if provided, else adapt based on command type."""
    if user_timeout is not None:
        return user_timeout

    # Very long-running operations (20 minutes) - based on typical cloud provider timings:
    # - Kubernetes clusters (GKE: 3-6min create/delete, EKS: 10-20min, AKS: 10-18min)
    # - Database instances (Cloud SQL: 5-30min, RDS: 5-45min, Azure SQL: 5-20min)
    # - Database restores (can take 20-45min)
    very_long_ops = [
        "cluster create", "clusters create", "create-cluster", "create cluster",  # K8s cluster creation
        "cluster delete", "clusters delete", "delete-cluster", "delete cluster",  # K8s cluster deletion
        "sql instances create", "instances create",  # GCP Cloud SQL creation
        "sql instances delete", "instances delete",  # GCP Cloud SQL deletion
        "rds create-db-instance", "create-db-instance",  # AWS RDS creation
        "rds delete-db-instance", "delete-db-instance",  # AWS RDS deletion
        "sql db create", "sql server create",  # Azure SQL creation
        "sql db delete", "sql server delete",  # Azure SQL deletion
        "restore"  # Database restores
    ]
    if any(op in command.lower() for op in very_long_ops):
        return 1200

    # Regular long-running operations (5 minutes)
    long_ops = ["delete", "create", "update", "deploy", "apply", "install"]
    if any(word in command for word in long_ops):
        return 300

    # Quick operations (60 seconds)
    return 60


def _cloud_exec_aws_multi_account(
    user_id: str,
    connections: list,
    command: str,
    provider_preference: Optional[str] = None,
    timeout: Optional[int] = None,
    output_file: Optional[str] = None,
    fn_start: float = 0,
) -> str:
    """Execute an AWS CLI command across all connected accounts and merge results.

    Each account gets its own STS credentials; the command runs in parallel
    via ThreadPoolExecutor.  Results are returned as a JSON object keyed by
    account_id so the agent can reason about per-account output.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    current_mode = get_mode_from_context()
    allowed, read_only_message = ModeAccessController.ensure_cloud_command_allowed(
        current_mode,
        is_read_only_command(command),
        command,
    )
    if not allowed:
        logger.warning(read_only_message)
        return json.dumps({
            "success": False,
            "error": read_only_message,
            "code": "READ_ONLY_MODE",
            "multi_account": True,
            "command": command,
            "provider": "aws",
        })

    if not is_read_only_command(command):
        from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation
        from .cloud_tools import get_state_context
        summary_msg = summarize_cloud_command(command)
        state_context = get_state_context()
        context_session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None
        if not wait_for_user_confirmation(
            user_id=user_id,
            message=f"[ALL {len(connections)} accounts] {summary_msg}",
            tool_name="cloud_exec",
            session_id=context_session_id,
        ):
            return json.dumps({
                "success": False,
                "error": "User declined multi-account command execution",
                "multi_account": True,
                "command": command,
                "provider": "aws",
            })

    def _run_on_account(conn: dict) -> dict:
        account_id = conn.get("account_id", "unknown")
        region = conn.get("region") or "us-east-1"
        try:
            success, _region, auth_method, isolated_env = setup_aws_environment_isolated(
                user_id, selected_region=region, target_account_id=account_id
            )
            if not success:
                return {"account_id": account_id, "region": region, "success": False,
                        "error": "Failed to assume role"}

            cmd = command.strip()
            if not cmd.startswith("aws"):
                cmd = f"aws {cmd}"
            if "--region" not in cmd:
                cmd += f" --region {region}"
            if "--output" not in cmd and any(
                kw in cmd for kw in ["list", "describe", "get"]
            ):
                cmd += " --output json"

            effective_timeout = get_command_timeout(cmd, timeout)
            cmd_args = shlex.split(cmd)
            result = terminal_run(
                cmd_args, capture_output=True, text=True,
                timeout=effective_timeout, env=isolated_env,
            )
            return {
                "account_id": account_id,
                "region": region,
                "success": result.returncode == 0,
                "output": result.stdout.strip() if result.returncode == 0 else result.stderr.strip(),
                "return_code": result.returncode,
            }
        except Exception as e:
            logger.error("Multi-account exec failed for %s: %s", account_id, e)
            return {"account_id": account_id, "region": region, "success": False,
                    "error": str(e)[:300]}

    account_results = {}
    with ThreadPoolExecutor(max_workers=min(len(connections), 10)) as pool:
        futures = {pool.submit(_run_on_account, c): c["account_id"] for c in connections}
        for future in as_completed(futures):
            res = future.result()
            acct = res.pop("account_id")
            account_results[acct] = res

    all_success = all(r.get("success") for r in account_results.values())
    elapsed = time.perf_counter() - fn_start if fn_start else 0
    logger.info("TIME: cloud_exec AWS multi-account (%d accounts) completed in %.2fs",
                len(connections), elapsed)

    return json.dumps({
        "success": all_success,
        "multi_account": True,
        "accounts_queried": len(account_results),
        "command": command,
        "provider": "aws",
        "results_by_account": account_results,
    })


def cloud_exec(provider: str, command: str, user_id: Optional[str] = None, session_id: Optional[str] = None, provider_preference: Optional[str] = None, timeout: Optional[int] = None, output_file: Optional[str] = None, account_id: Optional[str] = None) -> str:
    """Run arbitrary command against *provider* (gcloud/kubectl/gsutil for GCP, aws/kubectl for AWS).

CLI is very versatile and can be used to do the following things. It should be priority over IaC tools unless it can't be done or better done with IaC.
Here are some examples of what you can do with the CLI:

Compute
 • launch, describe, resize, stop, start, reboot and delete virtual machines or compute instances  
 • manage machine types, images, and auto‑scaling groups  

Storage
 • create, list, upload to, download from, and delete object storage buckets and blobs  
 • attach, snapshot, and manage block‑storage volumes  

Networking
 • create, list, and delete virtual networks, subnets, and routing tables  
 • configure firewalls, security groups, and load balancers  
 • allocate, associate, and release public IP addresses  

Identity & Access
 • create, list, attach, detach, and delete IAM roles, groups, and policies  
 • generate and rotate access keys, service account credentials, and tokens  

Container & Orchestration
 • deploy, list, update, and delete containers and container clusters  
 • manage container registries, push and pull images  

Serverless & Functions
 • deploy, invoke, list, update, and delete serverless functions or functions apps  

Databases & Data Services
 • create, list, scale, backup, restore, and delete managed database instances  
 • execute simple queries or import/export data  

Monitoring & Logging
 • fetch logs, metrics, and health‑check statuses  
 • set up alarms, dashboards, and notifications  

Configuration & Infrastructure as Code
 • apply, plan, and destroy infrastructure templates (e.g., Terraform)  
 • manage configuration parameters and secret stores  

Security & Compliance
 • audit resource configurations, check policy compliance, and remediate issues  
 • scan for vulnerabilities and apply security patches  

    """
    # Import tool capture functions once at the start
    from .cloud_tools import get_tool_capture, get_current_tool_call_id
    
    # Preserve original arguments for signature matching
    # The command will be modified (gcloud prefix, --format, --project flags added)
    # but we need original args to match with LangChain's tool call signature
    original_provider = provider
    original_command = command
    
    try:
        fn_start = time.perf_counter()  # Start timing for entire execution
        
        # Get user_id from context if not provided
        if not user_id:
            from utils.cloud.cloud_utils import get_user_context
            context = get_user_context()
            user_id = context.get('user_id') if isinstance(context, dict) else context
            if not user_id:
                return json.dumps({"error": "No user_id provided and no user context found", "final_command": command})
        
        # Get provider preference and selected project/region from context if not provided
        if not provider_preference:
            # First get the user's actual enabled providers from thread-local context
            from utils.cloud.cloud_utils import get_provider_preference
            thread_local_prefs = get_provider_preference()
            
            if thread_local_prefs and len(thread_local_prefs) > 0:
                # Use the user's enabled providers for context detection
                detected_provider = determine_target_provider_from_context(thread_local_prefs)
                logger.info(f"Detected provider from context (from {thread_local_prefs}): {detected_provider}")
                
                if detected_provider:
                    provider_preference = detected_provider
                else:
                    # No provider detected from context, use first enabled provider
                    provider_preference = thread_local_prefs[0]
                    logger.info(f"Using first enabled provider preference: {provider_preference}")
            else:
                return json.dumps({
                    "error": "No cloud provider detected from context. Please specify or connect a provider before running cloud tools.",
                    "requires_connection": True,
                    "success": False,
                    "final_command": command
                })
            selected_project_id = None
        else:
            selected_project_id = None
        logger.info(f"Provider preference: {provider_preference}")
        
        # Set up ISOLATED environment based on provider - NO GLOBAL STATE!
        isolated_env = None
        auth_command = None
        if provider.lower() in ['azure', 'az']:
            # Azure isolated setup
            success, subscription_id, auth_method, isolated_env, auth_command = setup_azure_environment_isolated(user_id, selected_project_id)
            if not success:
                return json.dumps({"error": f"Failed to setup Azure environment with {provider_preference} authentication", "final_command": command})
            resource_id = subscription_id
        elif provider.lower() == 'aws':
            # AWS multi-account: fan out only if no specific account_id given
            if not account_id:
                from utils.db.connection_utils import get_all_user_aws_connections
                all_conns = get_all_user_aws_connections(user_id) if user_id else []
                if len(all_conns) > 1:
                    return _cloud_exec_aws_multi_account(
                        user_id=user_id,
                        connections=all_conns,
                        command=original_command,
                        provider_preference=provider_preference,
                        timeout=timeout,
                        output_file=output_file,
                        fn_start=fn_start,
                    )
            # Single account path -- either account_id was given or only 1 connection
            success, region, auth_method, isolated_env = setup_aws_environment_isolated(
                user_id,
                selected_region=_get_region_for_account(user_id, account_id) if account_id else selected_project_id,
                target_account_id=account_id,
            )
            if not success:
                return json.dumps({"error": f"Failed to setup AWS environment with {provider_preference} authentication", "final_command": command})
            resource_id = region
        elif provider.lower() == 'ovh':
            # OVH isolated setup
            success, project_id, auth_method, isolated_env = setup_ovh_environment_isolated(user_id, selected_project_id)
            if not success:
                return json.dumps({"error": f"Failed to setup OVH environment with {provider_preference} authentication. Please connect your OVH account first.", "final_command": command, "requires_connection": True})
            resource_id = project_id
        elif provider.lower() == 'scaleway':
            # Scaleway isolated setup
            success, project_id, auth_method, isolated_env = setup_scaleway_environment_isolated(user_id, selected_project_id)
            if not success:
                return json.dumps({"error": f"Failed to setup Scaleway environment with {provider_preference} authentication. Please connect your Scaleway account first.", "final_command": command, "requires_connection": True})
            resource_id = project_id
        elif provider.lower() == 'tailscale':
            # Tailscale isolated setup - uses REST API, not CLI
            success, tailnet, auth_method, isolated_env = setup_tailscale_environment_isolated(user_id, selected_project_id)
            if not success:
                return json.dumps({"error": f"Failed to setup Tailscale environment. Please connect your Tailscale account first.", "final_command": command, "requires_connection": True})
            resource_id = tailnet
        else:
            # GCP isolated setup (default)
            success, project_id, auth_method, isolated_env = setup_gcp_environment_isolated(user_id, selected_project_id, provider_preference)
            if not success:
                return json.dumps({"error": f"Failed to setup GCP environment with {provider_preference} authentication", "final_command": command})
            resource_id = project_id

        logger.info(f"Using {auth_method} authentication for resource {resource_id}")
        
        # Use the resource_id as region_or_project for command building
        region_or_project = resource_id

        # ------------------------------------------------------------------
        # Resolve human-friendly resource_name for UI (project/subscription/account alias)
        # ------------------------------------------------------------------
        resource_name = None
        try:
            if provider.lower() in ['gcp', 'gcloud']:
                # Fetch friendly project name via Cloud Resource Manager; fallback to project_id
                token = (isolated_env.get("CLOUDSDK_AUTH_ACCESS_TOKEN") 
                         or isolated_env.get("GOOGLE_OAUTH_ACCESS_TOKEN"))
                if token and resource_id:
                    try:
                        r = requests.get(
                            f"https://cloudresourcemanager.googleapis.com/v1/projects/{resource_id}",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=5
                        )
                        if r.ok:
                            pj = r.json()
                            resource_name = pj.get("name") or pj.get("projectId") or resource_id
                    except Exception:
                        pass
                resource_name = resource_name or resource_id

            elif provider.lower() in ['aws', 'amazon']:
                # Prefer account alias from setup; fallback to account ID; append region without parentheses
                label = None
                try:
                    alias = (isolated_env.get("AURORA_AWS_ACCOUNT_ALIAS") or "").strip()
                    if alias:
                        label = alias
                    else:
                        acct_id = (isolated_env.get("AURORA_AWS_ACCOUNT_ID") or "").strip()
                        label = acct_id if acct_id else None
                except Exception:
                    label = None
                resource_name = f"{label} - {region_or_project}" if label else region_or_project

            elif provider.lower() in ['azure', 'az']:
                # Try to resolve subscription display name via ARM
                try:
                    current_mode = get_mode_from_context()
                    az_creds = generate_azure_access_token(user_id, subscription_id=resource_id, mode=current_mode)
                    az_token = az_creds.get("access_token")
                    if az_token and resource_id:
                        sresp = requests.get(
                            f"https://management.azure.com/subscriptions/{resource_id}?api-version=2020-01-01",
                            headers={"Authorization": f"Bearer {az_token}"},
                            timeout=5
                        )
                        if sresp.status_code == 200:
                            sdata = sresp.json()
                            resource_name = sdata.get("displayName") or resource_id
                    if not resource_name:
                        # Some credential stores keep subscription_name alongside id
                        resource_name = az_creds.get("subscription_name", resource_id)
                except Exception:
                    resource_name = resource_id

            elif provider.lower() == 'ovh':
                # Use project ID or endpoint as resource name
                endpoint = isolated_env.get("OVH_ENDPOINT", "ovh-us")
                if resource_id:
                    resource_name = f"OVH {endpoint} - {resource_id}"
                else:
                    resource_name = f"OVH {endpoint}"

            elif provider.lower() == 'scaleway':
                # Use project ID and region as resource name
                region = isolated_env.get("SCW_DEFAULT_REGION", "")
                if resource_id:
                    resource_name = f"Scaleway {region} - {resource_id}" if region else f"Scaleway - {resource_id}"
                else:
                    resource_name = f"Scaleway {region}" if region else "Scaleway"

            elif provider.lower() == 'tailscale':
                # Use tailnet name as resource name
                tailnet_name = isolated_env.get("TAILSCALE_TAILNET_NAME")
                if tailnet_name and tailnet_name != "-":
                    resource_name = f"Tailscale - {tailnet_name}"
                elif resource_id and resource_id != "-":
                    resource_name = f"Tailscale - {resource_id}"
                else:
                    resource_name = "Tailscale"
        except Exception:
            resource_name = resource_id
       
        # Special handling for 'gcloud config get-value project'
        # The gcloud CLI ignores environment variables for 'config get-value', which causes confusion
        # because we rely on GOOGLE_CLOUD_PROJECT env var for isolation.
        # We intercept this specific command to return the *effective* project ID.
        if provider.lower() in ['gcp', 'gcloud'] and region_or_project:
             normalized_cmd = command.strip()
             if not normalized_cmd.startswith("gcloud"):
                 normalized_cmd = f"gcloud {normalized_cmd}"
             
             if normalized_cmd == "gcloud config get-value project":
                 logger.info(f"Intercepting 'gcloud config get-value project' to return effective project: {region_or_project}")
                 return json.dumps({
                    "success": True,
                    "command": command,
                    "final_command": command,
                    "region_or_project": region_or_project,
                    "resource_id": resource_id,
                    "auth_method": auth_method,
                    "provider": provider.lower(),
                    "return_code": 0,
                    "chat_output": region_or_project,
                    "output": region_or_project + "\n"
                })

        # Check if this is a resource creation/modification command
        # Commented out to allow all commands
        #if not is_read_only_command(command):
        #    suggestion = suggest_iac_alternative(command)
        #    return json.dumps({
        #        "error": "Resource creation/modification commands are not allowed. Use IaC tools instead.",
        #        "suggestion": suggestion,
        #        "success": False,
        #        "auth_method": auth_method,
        #        "region_or_project": region_or_project
        #    })

        # Special handling for Tailscale - uses REST API instead of CLI
        if provider.lower() == 'tailscale':
            logger.info(f"Executing Tailscale command via REST API: {command}")

            # Check read-only mode
            current_mode = get_mode_from_context()
            if not is_read_only_command(command):
                allowed, read_only_message = ModeAccessController.ensure_cloud_command_allowed(
                    current_mode,
                    is_read_only_command(command),
                    command,
                )
                if not allowed:
                    logger.warning(read_only_message)
                    return json.dumps({
                        "success": False,
                        "error": read_only_message,
                        "code": "READ_ONLY_MODE",
                        "final_command": command,
                        "provider": provider.lower(),
                    })

            # Execute via REST API
            result = execute_tailscale_command(command, isolated_env)

            response = {
                "success": result.get("success", False),
                "command": command,
                "final_command": command,
                "region_or_project": region_or_project,
                "resource_id": resource_id,
                "resource_name": resource_name or "Tailscale",
                "auth_method": auth_method,
                "provider": "tailscale",
                "return_code": result.get("return_code", 1),
                "chat_output": result.get("chat_output", result.get("error", "Command executed")),
            }

            if not result.get("success"):
                response["error"] = result.get("error", "Unknown error")

            if result.get("output"):
                response["output"] = result.get("output")

            logger.info(f"TIME: cloud_exec (tailscale) completed in {time.perf_counter() - fn_start:.2f}s")
            return json.dumps(response)

        # For read-only commands, try to use cloud APIs directly first (disabled for performance)
        # The fast-path below adds significant latency due to per-zone API calls.
        # Temporarily disabled so we always fall back to the CLI, which is faster (~3 s).



        
        # Determine which CLI tool is being invoked based on provider
        import shlex as _shlex  # local alias to avoid clobbering the imported shlex at top
        try:
            _first_token = _shlex.split(command)[0].lower() if command.strip() else ''
        except ValueError:
            _first_token = ''  # fall back gracefully if parsing fails here – will be handled later

        # Define supported CLI tools for each provider
        if provider.lower() in ['gcp', 'gcloud']:
            supported_cli_tools = ['gcloud', 'kubectl', 'gsutil', 'bq', 'helm', 'terraform']
            default_cli = 'gcloud'
        elif provider.lower() in ['aws', 'amazon']:
            supported_cli_tools = ['aws', 'kubectl', 'eksctl', 'sam', 'cdk', 'helm', 'terraform']
            default_cli = 'aws'
        elif provider.lower() in ['azure', 'az']:
            supported_cli_tools = ['az', 'kubectl', 'helm', 'terraform']
            default_cli = 'az'
        elif provider.lower() == 'ovh':
            supported_cli_tools = ['ovhcloud', 'kubectl', 'helm', 'terraform']
            default_cli = 'ovhcloud'
        elif provider.lower() == 'scaleway':
            supported_cli_tools = ['scw', 'kubectl', 'helm', 'terraform']
            default_cli = 'scw'
        else:
            supported_cli_tools = []
            default_cli = ''

        cli_tool = _first_token if _first_token in supported_cli_tools else default_cli

        # Terraform should run without provider-specific prefixing
        terraform_invocation = cli_tool == 'terraform'

        # If the command does not start with a recognised CLI tool, default to provider CLI
        if provider.lower() in ['gcp', 'gcloud'] and cli_tool == 'gcloud' and not terraform_invocation and not command.strip().startswith('gcloud'):
            command = f"gcloud {command}"
        elif provider.lower() in ['aws', 'amazon'] and cli_tool == 'aws' and not terraform_invocation and not command.strip().startswith('aws'):
            command = f"aws {command}"
        elif provider.lower() in ['azure', 'az'] and cli_tool == 'az' and not terraform_invocation and not command.strip().startswith('az'):
            command = f"az {command}"
        elif provider.lower() == 'ovh' and cli_tool == 'ovhcloud' and not terraform_invocation and not command.strip().startswith('ovhcloud'):
            command = f"ovhcloud {command}"
        elif provider.lower() == 'scaleway' and cli_tool == 'scw' and not terraform_invocation and not command.strip().startswith('scw'):
            command = f"scw {command}"

        # Apply provider-specific convenience flags
        if provider.lower() in ['gcp', 'gcloud'] and cli_tool == 'gcloud':
            # GCP-specific flags - ALWAYS use explicit project (NO global CLI state)
            # BUT skip for 'gcloud config' commands which manage configuration, not resources
            is_config_command = 'gcloud config' in command

            if region_or_project and '--project' not in command and not is_config_command:
                command += f" --project={region_or_project}"
                logger.info(f"Using explicit project: {region_or_project}")
            else:
                # Extract the project from the command if specified
                import re
                project_match = re.search(r'--project[=\s]+([^\s]+)', command)
                if project_match:
                    specified_project = project_match.group(1)
                    logger.info(f"Using user-specified project: {specified_project}")

            # Add format json for better parsing if not already specified
            if '--format' not in command and ('list' in command or 'describe' in command):
                command += " --format=json"

            # Add --quiet flag for deletion commands to avoid prompts
            if 'delete' in command and '--quiet' not in command and '-q' not in command:
                command += " --quiet"
                
        elif provider.lower() in ['aws', 'amazon'] and cli_tool == 'aws':
            # AWS-specific flags - ALWAYS use explicit region (NO global CLI state)
            if region_or_project and '--region' not in command:
                command += f" --region {region_or_project}"
                logger.info(f"Using explicit region: {region_or_project}")
            else:
                # Extract the region from the command if specified
                import re
                region_match = re.search(r'--region[=\s]+([^\s]+)', command)
                if region_match:
                    specified_region = region_match.group(1)
                    logger.info(f"Using user-specified region: {specified_region}")

            # Add output json for better parsing if not already specified
            if '--output' not in command and ('list' in command or 'describe' in command or 'get' in command):
                command += " --output json"

                
        elif provider.lower() in ['azure', 'az'] and cli_tool == 'az':
            # Azure: do not auto-append --subscription; rely on default set via 'az account set' or user-provided flags
            if '--output' not in command and '-o' not in command and ('list' in command or 'show' in command):
                command += " --output=json"

        elif provider.lower() == 'ovh' and cli_tool == 'ovhcloud':
            # OVH-specific flags - add JSON output for better parsing
            # Exclude kubeconfig commands - they need raw YAML output for kubectl
            if '--json' not in command and '-j' not in command and ('list' in command or 'get' in command) and 'kubeconfig' not in command:
                command += " --json"

        elif provider.lower() == 'scaleway' and cli_tool == 'scw':
            # Scaleway-specific flags - add JSON output for better parsing
            # Exclude kubeconfig commands - they need raw YAML output for kubectl
            if '-o' not in command and '--output' not in command and ('list' in command or 'get' in command) and 'kubeconfig' not in command:
                command += " -o json"
                
        # --- Auto-inject impersonation flag for gsutil ---------------------------------
        if provider.lower() in ['gcp', 'gcloud'] and cli_tool == 'gsutil' and auth_method == 'impersonated':
            # Use whichever env-var we previously set to retrieve SA email.
            sa_email = (
                os.environ.get("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT") or
                os.environ.get("CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT")
            )
            if sa_email and "-i" not in command.split():
                # Prepend the -i flag right after 'gsutil'
                if command.startswith('gsutil'):
                    command = command.replace('gsutil', f'gsutil -i {sa_email}', 1)
                else:
                    # In improbable cases where command starts with e.g. 'sudo gsutil'
                    tokens = command.split()
                    for idx, tok in enumerate(tokens):
                        if tok == 'gsutil':
                            tokens.insert(idx + 1, '-i')
                            tokens.insert(idx + 2, sa_email)
                            command = ' '.join(tokens)
                            break
                logger.info(f"Injected impersonation flag for gsutil: {sa_email}")
                # Add --quiet flag for deletion commands to avoid prompts
                if 'delete' in command and '--quiet' not in command and '-q' not in command:
                    command += " --quiet"

        logger.info(f"Executing command: {command}")

        current_mode = get_mode_from_context()
        allowed, read_only_message = ModeAccessController.ensure_cloud_command_allowed(
            current_mode,
            is_read_only_command(command),
            command,
        )
        if not allowed:
            logger.warning(read_only_message)
            return json.dumps({
                "success": False,
                "error": read_only_message,
                "code": "READ_ONLY_MODE",
                "final_command": command,
                "provider": provider.lower(),
            })

        # If command is potentially action, ask for confirmation first (after command processing)
        if not is_read_only_command(command):
            summary_msg = summarize_cloud_command(command)
            # Get session_id from context for state saving
            from .cloud_tools import get_state_context
            state_context = get_state_context()
            context_session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None
            
            if not wait_for_user_confirmation(
                user_id=user_id, 
                message=summary_msg, 
                tool_name="cloud_exec",
                session_id=context_session_id
            ):
                # Capture the cancellation result in the tool capture system
                tool_capture = get_tool_capture()
                current_tool_call_id = get_current_tool_call_id(
                    tool_name="cloud_exec",
                    tool_kwargs={'provider': original_provider, 'command': original_command}
                )
                
                cancellation_result = json.dumps({
                    "status": "cancelled",
                    "message": "cloud_exec command cancelled by user",
                    "chat_output": "Command cancelled.",
                    "user_cancelled": True,
                    "final_command": command,  # Now includes the processed command with all prefixes and flags
                })
                
                # Capture the cancellation result if we have a tool capture and current tool call ID
                if tool_capture and current_tool_call_id:
                    logger.info(f"Capturing cancellation result for tool call {current_tool_call_id}")
                    tool_capture.capture_tool_end(current_tool_call_id, cancellation_result, is_error=False)
                
                return cancellation_result
        
        # Check if CLI tool is available before attempting to execute
        if not check_cli_availability(cli_tool):
            logger.error(f"CLI tool '{cli_tool}' is not available")
            return json.dumps({
                "success": False,
                "error": f"CLI tool '{cli_tool}' is not installed or not available in PATH",
                "cli_tool": cli_tool,
                "provider": provider.lower(),
                "command": command,
                "final_command": command,
                "region_or_project": region_or_project,
                "auth_method": auth_method
            })
        
        # Use shlex.split() to properly handle quoted arguments
        try:
            command_args = shlex.split(command)
        except ValueError as e:
            logger.error(f"Failed to parse command: {e}")
            return json.dumps({
                "success": False,
                "error": f"Command parsing failed: {e}",
                "command": command,
                "final_command": command,
                "region_or_project": region_or_project,
                "resource_id": resource_id,
                "resource_name": resource_id,
                "auth_method": auth_method
            })
        
        # Determine adaptive timeout
        effective_timeout = get_command_timeout(command, timeout)
        
        # For Azure, we need to handle authentication differently
        # Execute auth and user command sequentially to preserve argument quoting
        if provider.lower() in ['azure', 'az'] and auth_command:
            # First, execute the authentication command
            try:
                auth_result = terminal_run(
                    shlex.split(auth_command),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=isolated_env
                )
                if auth_result.returncode != 0:
                    logger.error(f"Azure authentication failed: {auth_result.stderr}")
                    return json.dumps({
                        "success": False,
                        "error": f"Azure authentication failed: {auth_result.stderr}",
                        "command": command,
                        "final_command": command
                    })
                logger.info("Azure authentication successful")
            except Exception as auth_error:
                logger.error(f"Azure authentication error: {auth_error}")
                return json.dumps({
                    "success": False,
                    "error": f"Azure authentication error: {str(auth_error)}",
                    "command": command,
                    "final_command": command
                })
            
            # Now execute the user command with preserved arguments
            exec_command = command_args
        else:
            # Use direct command for GCP and AWS
            exec_command = command_args
        
        # Execute the command
        exec_start = time.perf_counter()
        try:
            result = terminal_run(
                exec_command,  # Use chained command for Azure, direct for others
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=isolated_env  # Use ISOLATED environment - NO global state!
            )
            logger.info(f"TIME: cloud command execution took {time.perf_counter() - exec_start:.2f}s")
        except FileNotFoundError as e:
            logger.error(f"CLI tool '{cli_tool}' not found: {e}")
            return json.dumps({
                "success": False,
                "error": f"CLI tool '{cli_tool}' not found. This may indicate it's not installed or not in PATH.",
                "cli_tool": cli_tool,
                "provider": provider.lower(),
                "command": command,
                "final_command": command,
                "region_or_project": region_or_project,
                "resource_id": resource_id,
                "resource_name": resource_id,
                "auth_method": auth_method,
                "details": str(e)
            })
        except Exception as e:
            logger.error(f"Unexpected error executing command: {e}")
            return json.dumps({
                "success": False,
                "error": f"Unexpected error executing command: {str(e)}",
                "command": command,
                "final_command": command,
                "region_or_project": region_or_project,
                "resource_id": resource_id,
                "resource_name": resource_id,
                "auth_method": auth_method
            })
        
        # Add debug logging for specific commands and failed Scaleway commands
        if any(keyword in command for keyword in ["compute instances", "ec2 describe-instances", "ecs list-clusters"]):
            logger.info(f"Cloud command executed - Return Code: {result.returncode}")
            logger.info(f"STDOUT: {result.stdout}")
            logger.info(f"STDERR: {result.stderr}")
        
        # Always log Scaleway command failures for debugging
        if provider.lower() == 'scaleway' and result.returncode != 0:
            logger.warning(f"Scaleway command failed - Return Code: {result.returncode}")
            logger.warning(f"Scaleway STDOUT: {result.stdout}")
            logger.warning(f"Scaleway STDERR: {result.stderr}")
        
        serial_port_hint = _extract_serial_port_pagination_hint(result.stderr)
        from .stderr_error_detector import detect_errors_in_stderr
        
        # Check stderr for real errors even if the CLI returned 0
        has_stderr_error, stderr_error_message = detect_errors_in_stderr(result.stderr)
        
        # Determine success based on exit code AND absence of stderr errors
        actual_success = result.returncode == 0 and not has_stderr_error
        
        # Parse the response
        # For successful commands, prefer STDOUT but include benign STDERR messages if STDOUT is empty
        # (Many cloud CLIs write success confirmations to STDERR)
        is_serial_port = "get-serial-port-output" in command
        if actual_success:
            if is_serial_port:
                # Use stdout if it has content, otherwise provide meaningful message
                if result.stdout and result.stdout.strip():
                    chat_output = _sanitize_no_truncate(result.stdout)
                else:
                    # No new output since the --start= position
                    chat_output = "No new serial port output since the specified position."
            else:
                chat_output = result.stdout.strip()
                if not chat_output and result.stderr.strip() and not has_stderr_error:
                    # STDOUT is empty but STDERR has benign messages (e.g., "Deleted [https://...]")
                    chat_output = result.stderr.strip()
                chat_output = sanitize_command_output(chat_output) if chat_output else "Command executed successfully"
            
            # Preserve pagination hint (place at the end so it survives model summarization)
            if serial_port_hint:
                chat_output = f"{chat_output}\n\n[stderr note] {serial_port_hint}"
        else:
            # For failure cases, try to extract meaningful error message
            filtered_stderr = filter_error_messages(result.stderr)
            
            # For OVH, check stdout first as it may contain the actual error JSON
            if provider.lower() == "ovh" and result.stdout and result.stdout.strip():
                try:
                    # OVH API errors may come as JSON in stdout
                    stdout_json = json.loads(result.stdout)
                    if isinstance(stdout_json, dict) and stdout_json.get("message"):
                        chat_output = f"OVH API Error: {stdout_json.get('message')}"
                    else:
                        chat_output = sanitize_command_output(result.stdout)
                except json.JSONDecodeError:
                    chat_output = sanitize_command_output(result.stdout)
            # For Scaleway, combine stdout and stderr for better error visibility
            elif provider.lower() == "scaleway":
                error_parts = []
                if result.stdout and result.stdout.strip():
                    error_parts.append(f"stdout: {result.stdout.strip()}")
                if result.stderr and result.stderr.strip():
                    error_parts.append(f"stderr: {result.stderr.strip()}")
                if error_parts:
                    chat_output = " | ".join(error_parts)
                else:
                    chat_output = f"Scaleway command failed with exit code {result.returncode}"
            else:
                chat_output = sanitize_command_output(filtered_stderr or result.stdout)
            
            # If still empty or just whitespace, provide a fallback message
            if not chat_output or not chat_output.strip():
                if provider.lower() == "ovh":
                    chat_output = f"OVH command failed with exit code {result.returncode}. Check the OVH console for details."
                elif provider.lower() == "scaleway":
                    chat_output = f"Scaleway command failed with exit code {result.returncode}. Check the Scaleway console for details."
                else:
                    chat_output = f"Command failed with exit code {result.returncode}."

        # If output_file is specified, write raw stdout to file (useful for kubeconfig, etc.)
        if output_file and actual_success and result.stdout:
            try:
                output_path = Path(output_file)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(result.stdout)
                logger.info(f"Wrote command output to file: {output_file}")
            except Exception as write_err:
                logger.error(f"Failed to write output to {output_file}: {write_err}")
                # Don't fail the command, just note it in the response
                chat_output = f"{chat_output}\n\nWarning: Failed to write output to {output_file}: {write_err}"

        response = {
            "success": actual_success,
            "command": command,
            "final_command": command,  # Add final_command field
            "region_or_project": region_or_project,

            "resource_id": resource_id,

            "resource_name": resource_name or resource_id,

            "auth_method": auth_method,
            "provider": provider.lower(),
            "return_code": result.returncode,
            "chat_output": chat_output
        }
        
        # Add output_file to response if specified
        if output_file and actual_success:
            response["output_file"] = output_file
        
        # If STDERR contains errors, add them to the response
        if has_stderr_error:
            response["error"] = stderr_error_message
            logger.warning(f"Command had exit code {result.returncode} but STDERR contained errors: {stderr_error_message}")
        
        if actual_success:
            try:
                # Try to parse as JSON if possible
                if result.stdout.strip():
                    output_data = json.loads(result.stdout)
                    
                    # No truncation here - LLM sees full output
                    # Frontend truncation is handled by send_tool_completion() via WebSocket
                    truncated_data = output_data
                    
                    # If it's a large response, summarize it based on provider and command type
                    if isinstance(truncated_data, list) and len(truncated_data) > 0:
                        # Summarize GCP compute instances
                        if "compute instances" in command:
                            summary = []
                            for instance in truncated_data:
                                if isinstance(instance, dict):
                                    summary.append({
                                        "name": instance.get("name"),
                                        "status": instance.get("status"),
                                        "machineType": instance.get("machineType", "").split("/")[-1] if instance.get("machineType") else None,
                                        "zone": instance.get("zone", "").split("/")[-1] if instance.get("zone") else None,
                                        "externalIP": instance.get("networkInterfaces", [{}])[0].get("accessConfigs", [{}])[0].get("natIP") if instance.get("networkInterfaces") else None,
                                        "internalIP": instance.get("networkInterfaces", [{}])[0].get("networkIP") if instance.get("networkInterfaces") else None
                                    })
                            response["data"] = {
                                "resources": summary,
                                "total_count": len(summary),
                                "summary": f"Found {len(summary)} compute instances"
                            }
                        # Summarize AWS EC2 instances
                        elif "ec2 describe-instances" in command and "Reservations" in truncated_data:
                            summary = []
                            for reservation in truncated_data.get("Reservations", []):
                                for instance in reservation.get("Instances", []):
                                    public_ip = instance.get("PublicIpAddress")
                                    private_ip = instance.get("PrivateIpAddress")
                                    summary.append({
                                        "instanceId": instance.get("InstanceId"),
                                        "state": instance.get("State", {}).get("Name"),
                                        "instanceType": instance.get("InstanceType"),
                                        "availabilityZone": instance.get("Placement", {}).get("AvailabilityZone"),
                                        "publicIP": public_ip,
                                        "privateIP": private_ip,
                                        "keyName": instance.get("KeyName"),
                                        "launchTime": instance.get("LaunchTime")
                                    })
                            response["data"] = {
                                "resources": summary,
                                "total_count": len(summary),
                                "summary": f"Found {len(summary)} EC2 instances"
                            }
                        # Summarize AWS ECS clusters
                        elif "ecs list-clusters" in command and "clusterArns" in truncated_data:
                            clusters = truncated_data.get("clusterArns", [])
                            summary = [{"clusterArn": arn, "clusterName": arn.split("/")[-1]} for arn in clusters]
                            response["data"] = {
                                "resources": summary,
                                "total_count": len(summary),
                                "summary": f"Found {len(summary)} ECS clusters"
                            }
                        # Summarize AWS EKS clusters
                        elif "eks list-clusters" in command and "clusters" in truncated_data:
                            clusters = truncated_data.get("clusters", [])
                            summary = [{"clusterName": name} for name in clusters]
                            response["data"] = {
                                "resources": summary,
                                "total_count": len(summary),
                                "summary": f"Found {len(summary)} EKS clusters"
                            }
                        # Summarize OVH flavors (VM types) - these can be 100+ items
                        elif provider.lower() == "ovh" and ("list-flavors" in command or "reference list-flavors" in command):
                            summary = []
                            cheapest_flavors = []  # Track cheapest for chat_output
                            for flavor in truncated_data:
                                if isinstance(flavor, dict):
                                    # Filter to Linux flavors, skip Windows for cleaner output
                                    os_type = flavor.get("osType", "linux")
                                    if os_type == "linux" and flavor.get("available", True):
                                        flavor_entry = {
                                            "id": flavor.get("id"),  # CRITICAL: Include ID for instance creation
                                            "name": flavor.get("name"),
                                            "vcpus": flavor.get("vcpus"),
                                            "ram": flavor.get("ram"),
                                            "disk": flavor.get("disk"),
                                            "type": flavor.get("type"),
                                        }
                                        summary.append(flavor_entry)
                                        # Track cheapest options (s1-2, d2-2, b2-7 are typically cheapest)
                                        name = flavor.get("name", "")
                                        if name in ["s1-2", "d2-2", "b2-7", "b2-15"]:
                                            cheapest_flavors.append(flavor_entry)
                            # Sort by vcpus for easier reading
                            summary.sort(key=lambda x: (x.get("vcpus") or 0, x.get("ram") or 0))
                            response["data"] = {
                                "resources": summary[:10],  # Limit to 10 for LLM
                                "cheapest_options": cheapest_flavors[:4],  # Show cheapest with IDs
                                "total_count": len(truncated_data),
                                "shown_count": min(10, len(summary)),
                                "summary": f"Found {len(truncated_data)} flavors. Use the 'id' field (UUID) for --flavor parameter, NOT the name!"
                            }
                            # Build chat_output with actual IDs for cheapest flavors
                            cheapest_info = []
                            for f in cheapest_flavors[:3]:
                                cheapest_info.append(f"{f.get('name')}: id={f.get('id')}")
                            response["chat_output"] = (
                                f"Found {len(truncated_data)} VM flavors.\n"
                                f"⚠️ IMPORTANT: Use the 'id' field (UUID) for --flavor, NOT the name!\n"
                                f"Cheapest options:\n" + "\n".join(cheapest_info) if cheapest_info else "See data.resources for options"
                            )
                        # Summarize OVH images - these can be 100+ items
                        elif provider.lower() == "ovh" and ("list-images" in command or "reference list-images" in command):
                            summary = []
                            recommended_images = []  # Track recommended for chat_output
                            for image in truncated_data:
                                if isinstance(image, dict):
                                    name = image.get("name", "")
                                    image_id = image.get("id")
                                    # Focus on common Linux distros
                                    if any(distro in name.lower() for distro in ["ubuntu", "debian", "centos", "rocky", "alma"]):
                                        image_entry = {
                                            "id": image_id,  # CRITICAL: Include ID for instance creation
                                            "name": name,
                                        }
                                        summary.append(image_entry)
                                        # Track popular images (Ubuntu 24.04, Debian 12, etc.)
                                        if any(kw in name.lower() for kw in ["ubuntu 24", "ubuntu 22", "debian 12", "rocky 9"]):
                                            recommended_images.append(image_entry)
                            response["data"] = {
                                "resources": summary[:8],  # Limit to 8 for LLM
                                "recommended": recommended_images[:4],  # Show recommended with IDs
                                "total_count": len(truncated_data),
                                "shown_count": min(8, len(summary)),
                                "summary": f"Found {len(truncated_data)} images. Use the 'id' field (UUID) for --boot-from.image, NOT the name!"
                            }
                            # Build chat_output with actual IDs for recommended images
                            recommended_info = []
                            for img in recommended_images[:3]:
                                recommended_info.append(f"{img.get('name')}: id={img.get('id')}")
                            response["chat_output"] = (
                                f"Found {len(truncated_data)} images.\n"
                                f"⚠️ IMPORTANT: Use the 'id' field (UUID) for --boot-from.image, NOT the name!\n"
                                f"Recommended:\n" + "\n".join(recommended_info) if recommended_info else "See data.resources for options"
                            )
                        # Summarize OVH regions
                        elif provider.lower() == "ovh" and "region list" in command:
                            summary = []
                            for region in truncated_data[:20]:
                                if isinstance(region, dict):
                                    summary.append({
                                        "name": region.get("name") or region.get("Name"),
                                        "status": region.get("status") or region.get("Status", "UP"),
                                        "continent": region.get("continentCode") or region.get("ContinentCode"),
                                    })
                                elif isinstance(region, str):
                                    summary.append({"name": region})
                            response["data"] = {
                                "resources": summary,
                                "total_count": len(truncated_data),
                                "summary": f"Found {len(truncated_data)} regions available"
                            }
                            response["chat_output"] = f"Found {len(truncated_data)} OVH regions. Common options: GRA7 (France), SBG5 (France), BHS5 (Canada), US-EAST-VA-1 (US)"
                        # Generic OVH list summarization for other large lists
                        elif provider.lower() == "ovh" and len(truncated_data) > 20:
                            # For any large OVH response, limit to 20 items
                            response["data"] = {
                                "items": truncated_data[:20],
                                "total_count": len(truncated_data),
                                "shown_count": 20,
                                "summary": f"Found {len(truncated_data)} items. Showing first 20."
                            }
                            response["chat_output"] = f"Found {len(truncated_data)} items (showing first 20)"
                        else:
                            # Check if list contains simple values (strings/numbers) vs objects
                            if truncated_data and isinstance(truncated_data[0], (str, int, float, bool)):
                                # Simple values - return as chat_output for plain display
                                response["chat_output"] = json.dumps(truncated_data, indent=2)
                            else:
                                # For other large responses with objects, use the truncated data directly
                                response["data"] = {
                                    "items": truncated_data,
                                    "count": len(truncated_data),
                                    "summary": f"Command executed successfully, returned {len(truncated_data)} items"
                                }
                    else:
                        response["data"] = truncated_data
                else:
                    response["message"] = "Command executed successfully"
                    response["output"] = sanitize_command_output(result.stdout)
            except json.JSONDecodeError:
                # If not JSON, return as text - no truncation here
                # Frontend truncation is handled by send_tool_completion() via WebSocket
                response["output"] = sanitize_command_output(result.stdout)
        else:
            response["error"] = sanitize_command_output(filter_error_messages(result.stderr) or result.stdout)
            
        # Soft limit: Log warning for large responses but DON'T block - LLM sees full output
        # Frontend truncation is handled by send_tool_completion() via WebSocket
        final_response = json.dumps(response, indent=2)
        response_tokens = count_tokens(final_response)
        
        # Proactive sizing threshold (token-based only; no row caps)
        # When responses exceed this, we auto-rerun with a projection preview.
        FILTER_TOKEN_THRESHOLD = 30000

        def _command_already_filtered(cmd: str) -> bool:
            lowered = cmd.lower()
            return any(flag in lowered for flag in ["--filter", "--query", "--limit", "--page-size", "--max-items"])

        def _build_projection_command(provider_name: str, cmd: str) -> Tuple[Optional[str], Optional[str]]:
            import re
            lowered = cmd.lower()
            if provider_name.lower() in ['gcp', 'gcloud']:
                if " list" in lowered:
                    base_cmd = re.sub(r'--format[=\s]+[^\s]+', '', cmd).strip()
                    projection = '--format="value(name,status)"'
                    logger.info(f"[cloud_exec] projection builder (gcp list generic): base_cmd={base_cmd}, projection={projection}")
                    return f"{base_cmd} {projection}", "Applied generic projection to reduce list output size"
            elif provider_name.lower() == 'aws':
                # Avoid provider-specific projections for AWS to prevent malformed queries on nested outputs.
                # Let the caller warn and fall back to UI truncation if no safe generic projection exists.
                return None, None
            elif provider_name.lower() in ['azure', 'az']:
                if " list" in lowered and "--query" not in lowered:
                    query = "[].{name:name,id:id,location:location}"
                    logger.info(f"[cloud_exec] projection builder (az list generic): query={query}")
                    return f"{cmd} --query \"{query}\" --output json", "Applied generic projection to reduce list output size"
            return None, None

        try:
            parsed_data = None
            if "data" in response:
                parsed_data = response["data"]
            elif "chat_output" in response:
                # Best-effort parse for arrays to count items
                try:
                    parsed_data = json.loads(response["chat_output"])
                except Exception:
                    parsed_data = None

            logger.info(f"[cloud_exec] sizing check: response_tokens={response_tokens}, threshold={FILTER_TOKEN_THRESHOLD}")
            needs_filter = response_tokens > FILTER_TOKEN_THRESHOLD
            already_filtered = _command_already_filtered(command)
            logger.info(f"[cloud_exec] filter decision: needs_filter={needs_filter}, already_filtered={already_filtered}, command={command}")

            if needs_filter and not already_filtered:
                try:
                    projection_cmd, projection_reason = _build_projection_command(provider, command)
                except Exception as build_err:
                    logger.warning(f"[cloud_exec] projection builder error: {build_err}")
                    projection_cmd, projection_reason = None, None

                logger.info(f"[cloud_exec] projection candidate: {projection_cmd} (reason={projection_reason})")

                if projection_cmd:
                    logger.warning(f"Large response ({response_tokens} tokens) for command: {command[:100]}... retrying with projection preview.")
                    try:
                        filtered_result = terminal_run(
                            shlex.split(projection_cmd),
                            capture_output=True,
                            text=True,
                            timeout=effective_timeout,
                            env=isolated_env
                        )
                        if filtered_result.returncode == 0:
                            filtered_output = filtered_result.stdout.strip() or filtered_result.stderr
                            filtered_output = sanitize_command_output(filtered_output)
                            response["filter_applied"] = True
                            response["filter_command"] = projection_cmd
                            response["filter_reason"] = projection_reason
                            response["original_command"] = command
                            response["original_chat_output"] = response.get("chat_output")
                            response["original_reference"] = "Full result available; rerun without projection or with a different filter if needed."
                            try:
                                filtered_data = json.loads(filtered_result.stdout)
                                filtered_data = truncate_json_fields(filtered_data, max_field_length=10000)
                                response["preview_data"] = filtered_data
                            except Exception:
                                response["preview_data"] = filtered_output
                            # Surface the projected output to UI while keeping original for reference
                            response["chat_output"] = filtered_output if isinstance(filtered_output, str) else str(filtered_output)
                            response["data"] = response.get("preview_data")
                            response["final_command"] = projection_cmd
                            final_response = json.dumps(response, indent=2)
                            response_tokens = count_tokens(final_response)
                        else:
                            logger.warning(f"Projection retry command failed (code {filtered_result.returncode}); returning original output.")
                    except Exception as retry_error:
                        logger.warning(f"Projection retry command errored: {retry_error}; returning original output.")
                else:
                    if projection_cmd:
                        response["filter_suggestion"] = {
                            "suggested_command": projection_cmd,
                            "reason": projection_reason
                        }
                    final_response = json.dumps(response, indent=2)
                    response_tokens = count_tokens(final_response)

            # If still large or filtering unavailable, attach guidance note
            if response_tokens > FILTER_TOKEN_THRESHOLD:
                response["large_output_note"] = (
                    f"Response is large ({response_tokens} tokens). "
                    "UI may truncate to ~10KB per field. Apply provider projections (e.g., --format/--query) to reduce payload size without limiting rows."
                )
                final_response = json.dumps(response, indent=2)
        except Exception as sizing_error:
            logger.debug(f"Output sizing/ filtering check failed: {sizing_error}")
            
        logger.info(f"TIME: cloud_exec completed in {time.perf_counter() - fn_start:.2f}s")
        
        # Capture the successful completion result in the tool capture system
        tool_capture = get_tool_capture()
        # Pass tool_name and tool_kwargs to match the correct parallel call by signature
        # CRITICAL: Use ORIGINAL arguments, not modified command
        current_tool_call_id = get_current_tool_call_id(
            tool_name="cloud_exec",
            tool_kwargs={'provider': original_provider, 'command': original_command}
        )
        
        # Capture the successful result if we have a tool capture and current tool call ID
        if tool_capture and current_tool_call_id:
            logger.info(f"Capturing successful completion result for tool call {current_tool_call_id}")
            tool_capture.capture_tool_end(current_tool_call_id, final_response, is_error=False)
        
        return final_response
        
    except subprocess.TimeoutExpired:
        # Capture the timeout result in the tool capture system
        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="cloud_exec",
            tool_kwargs={'provider': original_provider, 'command': original_command}
        )
        
        timeout_result = json.dumps({
            "error": "Command timed out after 30 seconds",
            "success": False,
            "command": command,
            "final_command": command, # Also add here for consistency
            "region_or_project": region_or_project,
            "resource_id": resource_id,
            "resource_name": resource_id,
            "auth_method": auth_method
        })
        
        # Capture the timeout result if we have a tool capture and current tool call ID
        if tool_capture and current_tool_call_id:
            logger.info(f"Capturing timeout result for tool call {current_tool_call_id}")
            tool_capture.capture_tool_end(current_tool_call_id, timeout_result, is_error=True)
        
        return timeout_result
    except Exception as e:
        logger.error(f"Error in cloud_exec: {e}")
        
        # Capture the exception result in the tool capture system
        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="cloud_exec",
            tool_kwargs={'provider': original_provider, 'command': original_command}
        )
        
        exception_result = json.dumps({
            "error": f"Cloud execution failed: {str(e)}",
            "success": False,
            "command": command if 'command' in locals() else "unknown",
            "final_command": command if 'command' in locals() else "unknown", # Also add here
            "region_or_project": region_or_project if 'region_or_project' in locals() else "unknown",
            "resource_id": resource_id if 'resource_id' in locals() else "unknown",
            "resource_name": resource_id if 'resource_id' in locals() else "unknown",
            "auth_method": auth_method if 'auth_method' in locals() else "unknown"
        })
        
        # Capture the exception result if we have a tool capture and current tool call ID
        if tool_capture and current_tool_call_id:
            logger.info(f"Capturing exception result for tool call {current_tool_call_id}")
            tool_capture.capture_tool_end(current_tool_call_id, exception_result, is_error=True)
        
        return exception_result

cloud_exec_tool = StructuredTool.from_function(cloud_exec) 
