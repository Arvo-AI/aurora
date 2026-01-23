# setup_terraform_environment.py
"""
Canonical utilities for setting up Terraform environments for GCP, AWS, and Azure.
Provides setup_terraform_environment and cloud-specific helpers.
"""
import os
import tempfile
import shutil
from typing import Dict, Any, Optional, Tuple
from routes.terraform.terraform_manager import TerraformManager
from routes.terraform.terraform_generator import TerraformGenerator
import logging

logger = logging.getLogger(__name__)


def setup_terraform_workdir(base_dir: Optional[str] = None) -> str:
    """
    Prepare a working directory for Terraform operations.
    If base_dir is not provided, a temporary directory is created.
    Returns the path to the working directory.
    """
    if base_dir:
        os.makedirs(base_dir, exist_ok=True)
        return base_dir
    return tempfile.mkdtemp()


def write_terraform_files_from_zip(zip_path: str, workdir: str, cloud_provider: str = "gcp") -> str:
    """
    Generate Terraform files from a source code zip and write them to the working directory.
    Returns the path to the generated main.tf file.
    """
    generator = TerraformGenerator(workdir)
    success, result = generator.generate_terraform_from_zip(zip_path, cloud_provider)
    if not success:
        raise RuntimeError(f"Failed to generate Terraform files: {result.get('error', 'Unknown error')}")
    # The generator writes files to workdir/terraform/<session_id>/main.tf
    # Find the most recent session directory
    tf_dir = os.path.join(workdir, 'terraform')
    if not os.path.isdir(tf_dir):
        raise RuntimeError("Terraform directory not found after generation.")
    session_dirs = [os.path.join(tf_dir, d) for d in os.listdir(tf_dir) if os.path.isdir(os.path.join(tf_dir, d))]
    if not session_dirs:
        raise RuntimeError("No session directory found after generation.")
    latest_dir = max(session_dirs, key=os.path.getmtime)
    main_tf = os.path.join(latest_dir, 'main.tf')
    if not os.path.isfile(main_tf):
        raise RuntimeError("main.tf not found after generation.")
    # Optionally copy main.tf to workdir root for TerraformManager
    shutil.copy2(main_tf, os.path.join(workdir, 'main.tf'))
    return os.path.join(workdir, 'main.tf')


def init_terraform(workdir: str, authenticator=None) -> bool:
    """
    Initialize Terraform in the given working directory.
    Returns True if successful, False otherwise.
    """
    manager = TerraformManager(workdir, authenticator)
    return manager.init()


def plan_terraform(workdir: str, authenticator=None) -> Tuple[bool, str]:
    """
    Run 'terraform plan' in the given working directory.
    Returns (success, output).
    """
    manager = TerraformManager(workdir, authenticator)
    return manager.plan()


def apply_terraform(workdir: str, authenticator=None) -> Tuple[bool, str]:
    """
    Run 'terraform apply' in the given working directory.
    Returns (success, output).
    """
    manager = TerraformManager(workdir, authenticator)
    return manager.apply()


def write_tfvars(data: Dict[str, Any], workdir: str) -> str:
    """
    Write a terraform.tfvars file in the working directory using the provided data.
    Returns the path to the tfvars file.
    """
    manager = TerraformManager(workdir)
    tfvars_path = os.path.join(workdir, 'terraform.tfvars')
    success = manager.generate_terraform_tfvars(data, tfvars_path)
    if not success:
        raise RuntimeError("Failed to generate terraform.tfvars file.")
    return tfvars_path


def setup_azure_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with Azure credentials - ISOLATED VERSION."""
    try:
        # Get selected project/subscription from context
        from chat.backend.agent.tools.cloud_tools import get_selected_project_id
        selected_subscription_id = get_selected_project_id()

        # Try cached Azure setup first (configures az CLI env and caches identity)
        try:
            from chat.backend.agent.tools.auth import setup_azure_environment_cached
            ok_cached, cached_subscription_id, _ = setup_azure_environment_cached(user_id, selected_subscription_id)
            if ok_cached and cached_subscription_id:
                # We still need to set ARM_* variables for Terraform provider
                from utils.auth.cloud_auth import generate_azure_access_token
                from utils.auth.token_management import get_token_data
                azure_creds = generate_azure_access_token(user_id, cached_subscription_id)
                tenant_id = azure_creds["tenant_id"]
                token_data = get_token_data(user_id, "azure")
                if not token_data or not isinstance(token_data, dict):
                    raise ValueError(f"No Azure credentials found for user {user_id}")
                client_id = token_data.get("client_id")
                client_secret = token_data.get("client_secret")
                if not all([tenant_id, client_id, client_secret, cached_subscription_id]):
                    raise ValueError("Incomplete Azure credentials for Terraform")
                os.environ["ARM_CLIENT_ID"] = str(client_id)
                os.environ["ARM_CLIENT_SECRET"] = str(client_secret)
                os.environ["ARM_SUBSCRIPTION_ID"] = str(cached_subscription_id)
                os.environ["ARM_TENANT_ID"] = str(tenant_id)
                os.environ["TF_VAR_subscription_id"] = str(cached_subscription_id)
                os.environ["TF_VAR_tenant_id"] = str(tenant_id)
                os.environ["TF_VAR_client_id"] = str(client_id)
                logger.info(f"Azure Terraform environment configured for subscription: {cached_subscription_id} (cached)")
                return True, cached_subscription_id, None
        except Exception as e:
            logger.debug(f"Cached Azure setup unavailable; falling back to direct setup: {e}")

        # Get Azure credentials
        from utils.auth.cloud_auth import generate_azure_access_token
        from utils.auth.token_management import get_token_data
        azure_creds = generate_azure_access_token(user_id, selected_subscription_id)
        access_token = azure_creds["access_token"]
        subscription_id = azure_creds["subscription_id"]
        tenant_id = azure_creds["tenant_id"]

        # Get the raw credentials for Terraform
        token_data = get_token_data(user_id, "azure")
        if not token_data or not isinstance(token_data, dict):
            raise ValueError(f"No Azure credentials found for user {user_id}")

        client_id = token_data.get("client_id")
        client_secret = token_data.get("client_secret")

        if not all([tenant_id, client_id, client_secret, subscription_id]):
            raise ValueError("Incomplete Azure credentials for Terraform")

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            # Terraform Azure provider environment variables
            "ARM_CLIENT_ID": str(client_id),
            "ARM_CLIENT_SECRET": str(client_secret),
            "ARM_SUBSCRIPTION_ID": str(subscription_id),
            "ARM_TENANT_ID": str(tenant_id),
            # Terraform variables
            "TF_VAR_subscription_id": str(subscription_id),
            "TF_VAR_tenant_id": str(tenant_id),
            "TF_VAR_client_id": str(client_id),
        }

        logger.info(f"Azure Terraform isolated environment configured for subscription: {subscription_id}")
        return True, subscription_id, isolated_env

    except Exception as e:
        logger.error(f"Failed to setup Azure Terraform environment: {e}")
        return False, None, None

# OLD GLOBAL FUNCTION - Use setup_azure_terraform_environment_isolated() instead
def setup_azure_terraform_environment(user_id: str):
    """DEPRECATED - Use isolated version for concurrent safety."""
    success, subscription_id, isolated_env = setup_azure_terraform_environment_isolated(user_id)
    return success, subscription_id, isolated_env

def setup_aws_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with AWS credentials - ISOLATED VERSION."""
    try:
        logger.info("Setting up AWS credentials for Terraform...")

        # Try cached AWS setup first with isolated environment for concurrency safety
        try:
            from chat.backend.agent.tools.auth import setup_aws_credentials_cached
            result = setup_aws_credentials_cached(user_id, return_isolated=True)
            if len(result) == 4:  # New format with isolated env
                ok_cached, cached_region, auth_method, isolated_env = result
                if ok_cached and cached_region and isolated_env:
                    logger.info(f"(cached) AWS credentials configured for Terraform in region: {cached_region} (isolated)")
                    return True, cached_region, isolated_env
            else:  # Fallback to old format
                ok_cached, cached_region, _ = result
                if ok_cached and cached_region:
                    logger.info(f"(cached) AWS credentials configured for Terraform in region: {cached_region}")
                    return True, cached_region, None
        except Exception as e:
            logger.debug(f"Cached AWS setup unavailable; falling back to direct setup: {e}")

        # Get AWS credentials from database
        from utils.auth.stateless_auth import get_credentials_from_db
        aws_credentials = get_credentials_from_db(user_id, "aws")
        if not aws_credentials:
            logger.error(f"No AWS credentials found for user {user_id}")
            return False, None, None

        # Validate required fields
        required_fields = ['aws_access_key_id', 'aws_secret_access_key']
        missing_fields = [field for field in required_fields if not aws_credentials.get(field)]
        if missing_fields:
            logger.error(f"Missing required AWS credential fields for user {user_id}: {missing_fields}")
            return False, None, None

        access_key_id = aws_credentials['aws_access_key_id']
        secret_access_key = aws_credentials['aws_secret_access_key']

        # Get regions - use selected_region if provided, otherwise use first from stored regions
        regions = aws_credentials.get('aws_regions', ['us-east-1'])
        if isinstance(regions, list) and regions:
            region = regions[0]  # Use first region for Terraform
        else:
            region = 'us-east-1'

        logger.info(f"Using AWS region for Terraform: {region}")

        # REMOVED GLOBAL STATE MODIFICATION - using isolated environment instead

        # Validate credentials by making a test call to AWS STS
        try:
            import boto3
            session = boto3.Session(
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region
            )
            sts = session.client('sts')
            identity = sts.get_caller_identity()
            account_id = identity['Account']
            logger.info(f"Successfully validated AWS credentials for account: {account_id}")

        except Exception as e:
            logger.error(f"AWS credentials validation failed: {e}")
            return False, None, None

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            # Terraform AWS provider environment variables
            "AWS_ACCESS_KEY_ID": access_key_id,
            "AWS_SECRET_ACCESS_KEY": secret_access_key,
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
            # Terraform variables
            "TF_VAR_region": region,
            "TF_VAR_access_key": access_key_id,
        }

        logger.info(f"AWS Terraform isolated environment configured for region: {region}")
        return True, region, isolated_env

    except Exception as e:
        logger.error(f"Failed to setup AWS Terraform environment: {e}")
        return False, None, None

# OLD GLOBAL FUNCTION - Use setup_aws_terraform_environment_isolated() instead  
def setup_aws_terraform_environment(user_id: str):
    """DEPRECATED - Use isolated version for concurrent safety."""
    success, region, isolated_env = setup_aws_terraform_environment_isolated(user_id)
    return success, region, isolated_env

def setup_gcp_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with GCP credentials - ISOLATED VERSION."""
    try:
        # Get provider preference from context
        from chat.backend.agent.tools.cloud_tools import get_selected_project_id
        from utils.auth.cloud_auth import generate_contextual_access_token
        from utils.auth.token_management import get_token_data
        selected_project_id = get_selected_project_id()

        # Try cached GCP impersonation first
        try:
            from chat.backend.agent.tools.auth import setup_gcp_impersonation_cached
            ok_cached, cached_project_id, _ = setup_gcp_impersonation_cached(
                user_id, selected_project_id=selected_project_id
            )
            if ok_cached and cached_project_id:
                # Ensure Terraform gets project variable
                os.environ["TF_VAR_project_id"] = cached_project_id
                # Also try to create credentials file for Terraform (best-effort)
                try:
                    from connectors.gcp_connector.auth.service_accounts import create_local_credentials_file
                    token_data = get_token_data(user_id, "gcp")
                    if token_data and token_data.get('refresh_token'):
                        credentials_file_path = create_local_credentials_file(token_data, cached_project_id)
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file_path
                        logger.info(f"Created credentials file for Terraform: {credentials_file_path}")
                    else:
                        logger.warning("Could not get token data for Terraform credentials file - missing refresh token")
                except Exception as e:
                    logger.warning(f"Failed to create credentials file for Terraform: {e}")
                return True, cached_project_id, None
        except Exception as e:
            logger.debug(f"Cached GCP setup unavailable; falling back to direct setup: {e}")

        # Set up GCP credentials for Terraform
        logger.info("Setting up GCP credentials for Terraform...")
        token_resp = generate_contextual_access_token(user_id, selected_project_id=selected_project_id)
        access_token = token_resp["access_token"]
        project_id = token_resp["project_id"]

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            # Terraform GCP provider environment variables  
            "GOOGLE_OAUTH_ACCESS_TOKEN": access_token,
            "GOOGLE_CLOUD_PROJECT": project_id,
            "TF_VAR_project_id": project_id,
        }

        # Method 2: Create credentials file for Terraform (more reliable)
        credentials_file_path = None
        try:
            from connectors.gcp_connector.auth.service_accounts import create_local_credentials_file
            token_data = get_token_data(user_id, "gcp")
            if token_data and token_data.get('refresh_token'):
                credentials_file_path = create_local_credentials_file(token_data, project_id)
                isolated_env["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file_path
                logger.info(f"Created credentials file for Terraform: {credentials_file_path}")
            else:
                logger.warning("Could not get token data for Terraform credentials file - missing refresh token")
        except Exception as e:
            logger.warning(f"Failed to create credentials file for Terraform: {e}")
            # Continue with access token only

        logger.info(f"GCP Terraform isolated environment configured for project: {project_id}")
        return True, project_id, isolated_env
    except Exception as e:
        logger.error(f"Failed to setup GCP Terraform environment: {e}")
        return False, None, None

# OLD GLOBAL FUNCTION - Use setup_gcp_terraform_environment_isolated() instead  
def setup_gcp_terraform_environment(user_id: str):
    """DEPRECATED - Use isolated version for concurrent safety."""
    success, project_id, isolated_env = setup_gcp_terraform_environment_isolated(user_id)
    return success, project_id, isolated_env

def setup_ovh_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with OVH credentials - ISOLATED VERSION.
    
    Uses OAuth2 access token from existing OVH connection.
    Only requires the OVH provider (not OpenStack) for managed services like K8s.
    """
    try:
        logger.info("Setting up OVH credentials for Terraform...")

        # Get valid OVH access token (auto-refreshes if needed)
        from routes.ovh.oauth2_auth_code_flow import get_valid_access_token
        token_data = get_valid_access_token(user_id)
        
        if not token_data:
            logger.error(f"No OVH credentials found for user {user_id}")
            return False, None, None

        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint', 'ovh-eu')
        
        if not access_token:
            logger.error("OVH token data missing access_token")
            return False, None, None

        # Get user's OVH root project (service_name in OVH API terms)
        from utils.db.db_utils import connect_to_db_as_user
        project_id = None
        try:
            conn = connect_to_db_as_user()
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                cur.execute(
                    "SELECT preference_value FROM user_preferences WHERE user_id = %s AND preference_key = 'ovh_root_project';",
                    (user_id,)
                )
                row = cur.fetchone()
                if row:
                    project_id = row[0]
            conn.close()
        except Exception as e:
            logger.warning(f"Could not fetch OVH root project preference: {e}")

        if not project_id:
            logger.warning("No OVH root project set - Terraform may need project_id variable")

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            # OVH Terraform provider uses access token
            "OVH_ENDPOINT": endpoint,
            "OVH_ACCESS_TOKEN": access_token,
            # Terraform variables
            "TF_VAR_ovh_endpoint": endpoint,
        }
        
        if project_id:
            isolated_env["TF_VAR_project_id"] = project_id
            isolated_env["TF_VAR_service_name"] = project_id  # OVH uses service_name for project

        logger.info(f"OVH Terraform isolated environment configured for endpoint: {endpoint}")
        return True, project_id, isolated_env

    except Exception as e:
        logger.error(f"Failed to setup OVH Terraform environment: {e}")
        return False, None, None


def setup_scaleway_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with Scaleway credentials - ISOLATED VERSION.
    
    Uses API key authentication (access_key + secret_key).
    See: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs
    """
    try:
        logger.info("Setting up Scaleway credentials for Terraform...")

        # Get Scaleway credentials from Vault
        from utils.auth.token_management import get_token_data
        from utils.db.db_utils import connect_to_db_as_user

        # Get the secret_key from Vault via token_data
        token_data = get_token_data(user_id, "scaleway")
        
        if not token_data:
            logger.error(f"No Scaleway credentials found for user {user_id}")
            return False, None, None

        secret_key = token_data.get('secret_key')
        if not secret_key:
            logger.error("Scaleway token data missing secret_key")
            return False, None, None

        # Get access_key and other info from database
        access_key = None
        organization_id = None
        project_id = None
        
        conn = None
        try:
            conn = connect_to_db_as_user()
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                
                # Get access_key (stored as client_id) and organization_id (subscription_id)
                cur.execute(
                    "SELECT client_id, subscription_id, subscription_name FROM user_tokens WHERE user_id = %s AND provider = 'scaleway';",
                    (user_id,)
                )
                row = cur.fetchone()
                if row:
                    access_key = row[0]  # client_id = access_key
                    organization_id = row[1]  # subscription_id = organization_id
                    project_id = row[2]  # subscription_name = default_project_id
                
                # Get user's Scaleway root project preference if set
                cur.execute(
                    "SELECT preference_value FROM user_preferences WHERE user_id = %s AND preference_key = 'scaleway_root_project';",
                    (user_id,)
                )
                pref_row = cur.fetchone()
                if pref_row and pref_row[0]:
                    project_id = pref_row[0]  # Override with user preference
        except Exception as e:
            logger.warning(f"Could not fetch Scaleway credentials from database: {e}")
        finally:
            if conn:
                conn.close()

        if not access_key:
            logger.error("Scaleway access_key not found in database")
            return False, None, None

        if not project_id:
            logger.warning("No Scaleway project set - Terraform may need project_id variable")

        # Get region from token data or use default
        # Scaleway regions: fr-par (Paris), nl-ams (Amsterdam), pl-waw (Warsaw)
        # Terraform provider requires a region, so we must provide a default fallback
        region = token_data.get('default_region', 'fr-par')
        zone = token_data.get('default_zone', f"{region}-1")

        # BUILD ISOLATED ENVIRONMENT - NO global os.environ modification!
        # See: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs#environment-variables
        isolated_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "USER": os.environ.get("USER", ""),
            # Scaleway Terraform provider environment variables
            "SCW_ACCESS_KEY": access_key,
            "SCW_SECRET_KEY": secret_key,
            "SCW_DEFAULT_REGION": region,
            "SCW_DEFAULT_ZONE": zone,
            # Terraform variables
            "TF_VAR_region": region,
            "TF_VAR_zone": zone,
        }
        
        if project_id:
            isolated_env["SCW_DEFAULT_PROJECT_ID"] = project_id
            isolated_env["TF_VAR_project_id"] = project_id
            
        if organization_id:
            # Organization ID is used by Scaleway CLI, not typically needed for Terraform
            isolated_env["SCW_DEFAULT_ORGANIZATION_ID"] = organization_id

        logger.info(f"Scaleway Terraform isolated environment configured for region: {region}, zone: {zone}, project: {project_id}")
        return True, project_id, isolated_env

    except Exception as e:
        logger.error(f"Failed to setup Scaleway Terraform environment: {e}")
        return False, None, None


def setup_terraform_environment(user_id: str):
    """Set up Terraform environment with appropriate cloud provider credentials."""
    try:
        # Get provider preference from database (same source as frontend)
        provider_preference = None
        if user_id:
            try:
                from utils.auth.stateless_auth import get_connected_providers
                providers = get_connected_providers(user_id)
                if providers:
                    logger.info(f"Fetched connected providers from database: {providers}")
                    provider_preference = providers
            except Exception as e:
                logger.warning(f"Error fetching connected providers from database: {e}")

        # If no database preference, try thread-local context as fallback
        if not provider_preference:
            try:
                from utils.cloud.cloud_utils import get_provider_preference
                provider_preference = get_provider_preference()
                logger.info(f"Using thread-local provider preference: {provider_preference}")
            except Exception:
                pass

        # If still no preference, require explicit user selection
        if not provider_preference:
            logger.error("No provider preference found in database or context. User must select a cloud provider before running Terraform operations.")
            return False, None, None

        # Handle case where provider_preference is a list (multiple providers selected)
        if isinstance(provider_preference, list):
            if len(provider_preference) == 0:
                logger.error("No provider preference selected. User must select a cloud provider before running Terraform operations.")
                return False, None, None
            elif len(provider_preference) == 1:
                provider_preference = provider_preference[0]
                logger.info(f"Single provider selected: {provider_preference}")
            else:
                from utils.cloud.cloud_utils import get_user_context
                from chat.backend.agent.tools.cloud_tools import determine_target_provider_from_context
                user_context = get_user_context()
                target_provider = determine_target_provider_from_context(provider_preference)
                if target_provider:
                    provider_preference = target_provider
                    logger.info(f"Multiple providers selected: {provider_preference}. Using target provider '{target_provider}' based on user context.")
                else:
                    # No specific provider mentioned in context - choose a sensible default
                    # Default priority: gcp -> aws -> azure -> ovh -> scaleway
                    default_priority = ['gcp', 'aws', 'azure', 'ovh', 'scaleway']
                    default_provider = None
                    
                    for preferred in default_priority:
                        if preferred in provider_preference:
                            default_provider = preferred
                            break
                    
                    if not default_provider:
                        # Fallback to first available provider
                        default_provider = provider_preference[0]
                    
                    provider_preference = default_provider
                    logger.info(f"Multiple providers selected: {provider_preference}. No specific provider mentioned in context, using default: '{default_provider}'")

        # Ensure provider_preference is a string
        if not isinstance(provider_preference, str):
            logger.error("Invalid provider preference format")
            return False, None, None

        provider_preference = provider_preference.lower()
        logger.info(f"Setting up Terraform environment for provider: {provider_preference}")

        # Handle specific providers - USE ISOLATED VERSIONS
        if provider_preference.lower() == "azure":
            return setup_azure_terraform_environment_isolated(user_id)
        elif provider_preference.lower() == "aws":
            return setup_aws_terraform_environment_isolated(user_id)
        elif provider_preference.lower() == "ovh":
            return setup_ovh_terraform_environment_isolated(user_id)
        elif provider_preference.lower() == "scaleway":
            return setup_scaleway_terraform_environment_isolated(user_id)
        else:
            # Default to GCP for backwards compatibility
            return setup_gcp_terraform_environment_isolated(user_id)

    except Exception as e:
        logger.error(f"Failed to setup Terraform environment: {e}")
        return False, None, None


def setup_terraform_environment_isolated(user_id: str):
    """Set up Terraform environment with isolated credentials - returns (success, resource_id, isolated_env)."""
    return setup_terraform_environment(user_id)


def setup_terraform_environment_legacy(user_id: str):
    """Legacy wrapper for backward compatibility - returns only (success, resource_id)."""
    success, resource_id, isolated_env = setup_terraform_environment(user_id)
    return success, resource_id
