import base64
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from langchain_core.tools import StructuredTool
from ..cloud_provider_utils import determine_target_provider_from_context
from chat.backend.agent.iac_templates import (
    generate_gcp_provider_config,
    generate_aws_provider_config,
    generate_azure_provider_config,
    generate_ovh_provider_config,
    generate_scaleway_provider_config
)

logger = logging.getLogger(__name__)


def detect_provider_from_terraform_content(content: str) -> Optional[str]:
    """Detect the cloud provider from Terraform resource/data source prefixes in content.
    
    This is used when user mentions a provider with typos (e.g., "sacaleway" instead of "scaleway")
    but the LLM generates correct Terraform code with proper resource prefixes.
    
    Args:
        content: Terraform HCL content
        
    Returns:
        Detected provider name ('gcp', 'aws', 'azure', 'scaleway', 'ovh') or None
    """
    if not content:
        return None
    
    content_lower = content.lower()
    
    # Provider prefixes in Terraform resources/data sources
    # Order matters - check more specific patterns first
    provider_patterns = {
        'scaleway': [
            r'\bscaleway_',           # scaleway_instance_server, scaleway_vpc, etc.
            r'provider\s+"scaleway"',  # provider "scaleway" block
        ],
        'ovh': [
            r'\bovh_',                 # ovh_cloud_project, ovh_domain_zone, etc.
            r'provider\s+"ovh"',       # provider "ovh" block
        ],
        'azure': [
            r'\bazurerm_',             # azurerm_resource_group, azurerm_virtual_machine, etc.
            r'\bazuread_',             # azuread_user, azuread_group, etc.
            r'provider\s+"azurerm"',   # provider "azurerm" block
        ],
        'aws': [
            r'\baws_',                 # aws_instance, aws_vpc, etc.
            r'provider\s+"aws"',       # provider "aws" block
        ],
        'gcp': [
            r'\bgoogle_',              # google_compute_instance, google_storage_bucket, etc.
            r'\bgoogle-beta_',         # google-beta provider resources
            r'provider\s+"google"',    # provider "google" block
        ],
    }
    
    for provider, patterns in provider_patterns.items():
        for pattern in patterns:
            if re.search(pattern, content_lower):
                logger.info(f"Detected provider '{provider}' from Terraform content (pattern: {pattern})")
                return provider
    
    return None


def get_terraform_directory(user_id: Optional[str] = None, session_id: Optional[str] = None):
    """Get the directory for Terraform files, optionally user-specific and session-specific."""
    # Base terraform directory (use /home/appuser for terminal pods with read-only root filesystem)
    base_terraform_dir = Path("/home/appuser/terraform_workdir")
    
    # Build user-scoped path for isolation
    if user_id:
        # User IDs are now plain UUIDs without prefixes (Auth.js migration)
        # Always add user_ prefix to the directory name for consistency
        user_dir_name = f"user_{user_id}"
        user_terraform_dir = base_terraform_dir / user_dir_name
        
        # Add session subdirectory if provided
        if session_id:
            session_dir_name = f"session_{session_id}"
            session_terraform_dir = user_terraform_dir / session_dir_name
            return session_terraform_dir
        else:
            return user_terraform_dir
    else:
        # Fallback to base directory for backward compatibility
        return base_terraform_dir

# We expose `user_id` explicitly so that callers can pass it – this is
# important when the tool is executed outside the agent framework that
# automatically sets thread-local context.

def _resolve_project_id(user_id: str | None = None) -> str:
    """Return the best project_id we can determine at runtime.

    Order of precedence:
      1. Root project selected by the auth flow (via generate_contextual_access_token).
      2. Environment variables: TF_VAR_project_id or GOOGLE_CLOUD_PROJECT.
      3. Raise error if no project found (no fallback).

    Raises ValueError if no project can be resolved.
    """
    # ------------------------------------------------------------------
    # 1. Prefer the per-user root project determined by auth logic.
    # ------------------------------------------------------------------
    try:
        from utils.auth.cloud_auth import generate_contextual_access_token  # type: ignore
        if not user_id:
            # Try to get it from thread-local context if not explicitly passed  # type: ignore
            from utils.cloud.cloud_utils import get_user_context
            context = get_user_context()
            context_user_id = context.get('user_id') if isinstance(context, dict) else context
            if context_user_id:
                user_id = context_user_id

        if user_id:
            # Get selected project from context
            from ..cloud_tools import get_selected_project_id
            selected_project_id = get_selected_project_id()
            
            token_resp = generate_contextual_access_token(user_id, selected_project_id=selected_project_id)
            project_id = token_resp.get("project_id")
            if project_id:
                logger.info(f"Resolved project ID: {project_id}")
                return project_id
    except Exception:
        # Fall through to next strategies
        pass

    # ------------------------------------------------------------------
    # 2. Environment variable fallbacks
    # ------------------------------------------------------------------
    project_id = os.environ.get("TF_VAR_project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    
    # Prevent fallback to infrastructure project if it was injected by GKE/Workload Identity
    # This ensures we don't accidentally target the hosting project when user context fails
    if project_id == "sublime-flux-414616":
        logger.warning(f"Ignoring infrastructure project ID from environment: {project_id}")
        project_id = None
        
    if project_id:
        logger.info(f"Resolved project ID from env vars: {project_id}")
        return project_id

    # ------------------------------------------------------------------
    # 3. No project found - raise error
    # ------------------------------------------------------------------
    error_msg = (
        "No GCP project could be resolved. Please ensure you have a connected GCP account and selected a project"
    )
    logger.error(error_msg)
    raise ValueError(error_msg)

def _resolve_subscription_id(user_id: str | None = None) -> str:
    """Return the best subscription_id we can determine at runtime for Azure.

    Order of precedence:
      1. Root subscription selected by the auth flow (via generate_azure_access_token).
      2. Environment variables: TF_VAR_subscription_id or ARM_SUBSCRIPTION_ID.
      3. Default fallback subscription.

    The helper never raises – it will always return *some* string so that
    downstream logic keeps working even in degraded environments.
    """
    # ------------------------------------------------------------------
    # 1. Prefer the per-user subscription determined by auth logic.
    # ------------------------------------------------------------------
    try:
        from utils.auth.cloud_auth import generate_azure_access_token
        if not user_id:
            # Try to get it from thread-local context if not explicitly passed
            from utils.cloud.cloud_utils import get_user_context
            context = get_user_context()
            context_user_id = context.get('user_id') if isinstance(context, dict) else context
            if context_user_id:
                user_id = context_user_id

        if user_id:
            # Get selected subscription from context
            from ..cloud_tools import get_selected_project_id
            selected_subscription_id = get_selected_project_id()
            
            azure_creds = generate_azure_access_token(user_id, selected_subscription_id)
            subscription_id = azure_creds.get("subscription_id")
            if subscription_id:
                logger.info(f"Resolved subscription ID: {subscription_id}")
                return subscription_id
    except Exception:
        # Fall through to next strategies
        pass

    # ------------------------------------------------------------------
    # 2. Environment variable fallbacks
    # ------------------------------------------------------------------
    subscription_id = os.environ.get("TF_VAR_subscription_id") or os.environ.get("ARM_SUBSCRIPTION_ID")
    if subscription_id:
        logger.info(f"Resolved subscription ID from env vars: {subscription_id}")
        return subscription_id

    # ------------------------------------------------------------------
    # 3. Final fallback to a default subscription (you may want to customize this)
    # ------------------------------------------------------------------
    fallback_subscription = "00000000-0000-0000-0000-000000000000"  # Replace with actual default
    logger.warning(f"Using hardcoded fallback subscription ID: {fallback_subscription}")
    return fallback_subscription

def _resolve_aws_region(user_id: str | None = None) -> str:
    """Return the best AWS region we can determine at runtime.

    Order of precedence:
      1. Region from AWS credentials stored in database.
      2. Environment variables: TF_VAR_region, AWS_DEFAULT_REGION, or AWS_REGION.
      3. Default fallback region.

    The helper never raises – it will always return *some* string so that
    downstream logic keeps working even in degraded environments.
    """
    # ------------------------------------------------------------------
    # 1. Prefer the region from stored AWS credentials.
    # ------------------------------------------------------------------
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        if not user_id:
            # Try to get it from thread-local context if not explicitly passed
            from utils.cloud.cloud_utils import get_user_context
            context = get_user_context()
            context_user_id = context.get('user_id') if isinstance(context, dict) else context
            if context_user_id:
                user_id = context_user_id

        if user_id:
            aws_credentials = get_credentials_from_db(user_id, "aws")
            if aws_credentials:
                regions = aws_credentials.get('aws_regions', ['us-east-1'])
                if isinstance(regions, list) and regions:
                    region = regions[0]
                    logger.info(f"Resolved AWS region from credentials: {region}")
                    return region
    except Exception:
        # Fall through to next strategies
        pass

    # ------------------------------------------------------------------
    # 2. Environment variable fallbacks
    # ------------------------------------------------------------------
    region = os.environ.get("TF_VAR_region") or os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    if region:
        logger.info(f"Resolved AWS region from env vars: {region}")
        return region

    # ------------------------------------------------------------------
    # 3. Final fallback to a default region
    # ------------------------------------------------------------------
    fallback_region = "us-east-1"
    logger.warning(f"Using hardcoded fallback AWS region: {fallback_region}")
    return fallback_region

def _get_provider_preference_with_fallback(user_id: str | None = None):
    """Get provider preference from context, falling back to database if needed.
    
    This is a helper to avoid duplicate DB calls when multiple functions need
    the provider preference in the same flow.
    
    Returns:
        Provider preference from context or database, or None if not available
    """
    from utils.cloud_utils import get_provider_preference
    
    # First try context (set by agent at start of request)
    context_pref = get_provider_preference()
    if context_pref:
        return context_pref
    
    # Fallback to database if no context preference
    if not user_id:
        from utils.cloud_utils import get_user_context
        try:
            context = get_user_context()
            user_id = context.get('user_id') if isinstance(context, dict) else context
        except Exception:
            pass
    
    if user_id:
        try:
            from utils.auth.stateless_auth import get_connected_providers
            providers = get_connected_providers(user_id)
            if providers:
                logger.info(f"Fetched connected providers from database: {providers}")
                return providers
        except Exception as e:
            logger.warning(f"Error fetching connected providers from database: {e}")
    
    return None

def _resolve_resource_id(user_id: str | None = None, terraform_content: str | None = None) -> tuple[str, str]:
    """Resolve the appropriate resource ID and provider type.
    
    Args:
        user_id: User ID for credential lookup
        terraform_content: Optional Terraform content to detect provider from resource prefixes
    
    Returns:
        tuple: (resource_id, provider_type) where provider_type is 'gcp', 'azure', 'aws', 'scaleway', or 'ovh'
    """
    try:
        # Get provider preference from context, with database fallback
        provider_preference = _get_provider_preference_with_fallback(user_id)
        
        # If no database preference, try thread-local context as fallback
        if not provider_preference:
            try:
                from utils.cloud.cloud_utils import get_provider_preference
                provider_preference = get_provider_preference()
                logger.info(f"Using thread-local provider preference: {provider_preference}")
            except Exception:
                pass
        
        # If still no preference, this means user hasn't selected a provider
        # Don't make assumptions based on stored credentials - require explicit choice
        if not provider_preference:
            logger.error("No provider preference found in database or context. User must select a cloud provider before running Terraform operations.")
            raise ValueError("No provider preference found in database or context. User must select a cloud provider before running Terraform operations.")
        
        # Handle case where provider_preference is a list (multiple providers selected)
        if isinstance(provider_preference, list):
            if len(provider_preference) == 0:
                logger.error("No provider preference selected. User must select a cloud provider before running Terraform operations.")
                raise ValueError("No provider preference selected. User must select a cloud provider before running Terraform operations.")
            elif len(provider_preference) == 1:
                provider_preference = provider_preference[0]
                logger.info(f"Single provider selected: {provider_preference}")
            else:
                # Multiple providers selected - check if user has specified a target provider in context
                from utils.cloud.cloud_utils import get_user_context
                user_context = get_user_context()
                
                # PRIORITY 1: Detect provider from Terraform content (most reliable)
                # This handles typos like "sacaleway" -> but LLM generates correct "scaleway_" resources
                content_detected_provider = None
                if terraform_content:
                    content_detected_provider = detect_provider_from_terraform_content(terraform_content)
                    if content_detected_provider and content_detected_provider in provider_preference:
                        logger.info(f"Detected provider '{content_detected_provider}' from Terraform content - using this over context detection")
                        provider_preference = content_detected_provider
                
                # PRIORITY 2: Detect from user message context (handles explicit mentions)
                if isinstance(provider_preference, list):
                    target_provider = determine_target_provider_from_context(provider_preference)
                    logger.info(f"target_provider from context: {target_provider}")
                    if target_provider:
                        provider_preference = target_provider
                        logger.info(f"Multiple providers selected. Using target provider '{target_provider}' based on user context.")
                
                # PRIORITY 3: Fall back to default priority
                if isinstance(provider_preference, list):
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
                    logger.info(f"Multiple providers selected. No specific provider detected, using default: '{default_provider}'")
        
        # Ensure provider_preference is a string
        if not isinstance(provider_preference, str):
            logger.error(f"Invalid provider preference type: {type(provider_preference)}. Expected string or list of strings.")
            raise ValueError(f"Invalid provider preference type: {type(provider_preference)}. Expected string or list of strings.")
        
        logger.info(f"Resolved provider preference: {provider_preference}")
        
        if provider_preference.lower() == "azure":
            try:
                subscription_id = _resolve_subscription_id(user_id)
                return subscription_id, "azure"
            except Exception as e:
                logger.warning(f"Error resolving Azure subscription ID: {e}")
                # Return fallback values
                return "00000000-0000-0000-0000-000000000000", "azure"
        elif provider_preference.lower() == "aws":
            try:
                region = _resolve_aws_region(user_id)
                return region, "aws"
            except Exception as e:
                logger.warning(f"Error resolving AWS region: {e}")
                # Return fallback values
                return "us-east-1", "aws"
        elif provider_preference.lower() == "ovh":
            try:
                # For OVH, return the project_id from user preferences
                from utils.auth.stateless_auth import get_user_preference
                ovh_project_id = get_user_preference(user_id, 'ovh_project_id')
                if ovh_project_id:
                    return ovh_project_id, "ovh"
            except Exception as e:
                logger.warning(f"Error resolving OVH project ID: {e}")
                return "ovh-project", "ovh"
        elif provider_preference.lower() == "scaleway":
            try:
                # For Scaleway, return the project_id from user preferences
                # Full credential setup happens in setup_terraform_environment
                from utils.auth.stateless_auth import get_user_preference
                scaleway_project_id = get_user_preference(user_id, 'scaleway_root_project')
                if scaleway_project_id:
                    return scaleway_project_id, "scaleway"
                # Return placeholder - actual project_id will be set via TF_VAR_project_id env var
                return "scaleway-project", "scaleway"
            except Exception as e:
                logger.warning(f"Error resolving Scaleway project ID: {e}")
                return "scaleway-project", "scaleway"
        else:
            # Handle GCP-based project resolution
            # Let ValueError propagate - no fake fallback project IDs
            project_id = _resolve_project_id(user_id)
            return project_id, "gcp"
            
    except Exception as e:
        logger.error(f"Error resolving resource ID: {e}")
        raise ValueError(f"Failed to resolve resource ID and provider type: {e}")


def clear_terraform_state_if_provider_changed(user_id: Optional[str] = None, session_id: Optional[str] = None):
    """Clear terraform state if provider changed to avoid conflicts between ANY providers."""
    try:
        terraform_dir = get_terraform_directory(user_id, session_id)
        state_file = terraform_dir / "terraform.tfstate"
        lock_file = terraform_dir / ".terraform.lock.hcl"
        terraform_folder = terraform_dir / ".terraform"
        
        if not state_file.exists():
            logger.debug("No terraform state file exists, nothing to clear")
            return
            
        # Get provider preference from context, with database fallback
        current_provider_raw = _get_provider_preference_with_fallback(user_id)
        
        # Handle both single provider (string) and multiple providers (list) formats
        if isinstance(current_provider_raw, list) and len(current_provider_raw) > 0:
            current_provider = current_provider_raw[0]  # Use first provider for state management
        elif isinstance(current_provider_raw, str):
            current_provider = current_provider_raw
        else:
            current_provider = None
        
        if not current_provider:
            logger.debug("No provider preference set, clearing state to be safe")
            # Clear everything if no preference is set
            if state_file.exists():
                state_file.unlink()
            if lock_file.exists():
                lock_file.unlink()
            if terraform_folder.exists():
                import shutil
                shutil.rmtree(terraform_folder)
            logger.info("Cleared all terraform state due to missing provider preference")
            return
        
        # Read existing state to check what provider it was created with
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read terraform state file: {e}")
            # If we can't read the state, clear it to be safe
            if state_file.exists():
                state_file.unlink()
            return
        
        # Check if state has resources from different provider
        resources = state_data.get('resources', [])
        if not resources:
            logger.debug("No resources in state file")
            return
            
        # Determine what provider the state was created for
        state_provider = None
        for resource in resources:
            resource_type = resource.get('type', '')
            
            # Detect provider from resource types
            if resource_type.startswith('azurerm_'):
                state_provider = 'azure'
                break
            elif resource_type.startswith('aws_'):
                state_provider = 'aws'
                break
            elif resource_type.startswith('ovh_'):
                state_provider = 'ovh'
                break
            elif resource_type.startswith('scaleway_'):
                state_provider = 'scaleway'
                break
            elif resource_type.startswith('google_'):
                state_provider = 'gcp'
                break
        
        # Clear state if providers don't match
        if state_provider and state_provider != current_provider:
            logger.info(f"Provider switched from {state_provider} to {current_provider}, clearing terraform state")
            
            # Clear state file
            if state_file.exists():
                state_file.unlink()
                logger.info("Removed terraform.tfstate")
            
            # Clear lock file
            if lock_file.exists():
                lock_file.unlink()
                logger.info("Removed .terraform.lock.hcl")
            
            # Clear .terraform folder (contains provider plugins)
            if terraform_folder.exists():
                import shutil
                shutil.rmtree(terraform_folder)
                logger.info("Removed .terraform folder")
                
            logger.info(f"Successfully cleared terraform state for provider switch: {state_provider} -> {current_provider}")
        else:
            logger.debug(f"Provider unchanged ({current_provider}), keeping existing state")
            
    except Exception as e:
        logger.debug(f"Error checking/clearing terraform state: {e}")




def iac_write(path: str, content: str, user_id: Optional[str] = None, session_id: Optional[str] = None) -> str:
    """Create/overwrite IaC manifest at *path* with *content*.
    
    This tool can write any terraform content. This tool should only be used for complicated infrastructure management queries, not simple ones (those can be done with cloud_exec) OR if after using cloud_exec commands it failed. For common use cases, you can use these keywords
    to get started with helpful templates. DO NOT EVER USE THIS TOOL FOR DEPLOYING A VM IN AZURE.:
    
    GCP Resources:
    - VM/Compute Instance: google_compute_instance
    - GKE Cluster: google_container_cluster  
    - Cloud Run Service: google_cloud_run_service
    
    AWS Resources:
    - EC2 Instance: aws_instance
    - EKS Cluster: aws_eks_cluster
    - ECS Service: aws_ecs_service
    - Lambda Function: aws_lambda_function
    
    Azure Resources:
    - Virtual Machine: azurerm_linux_virtual_machine or azurerm_windows_virtual_machine
    - AKS Cluster: azurerm_kubernetes_cluster
    - App Service: azurerm_app_service
    
    CRITICAL: DO NOT include terraform{} blocks, required_providers{} blocks, or provider{} blocks in your content.
    The tool automatically generates the correct provider configuration with proper credentials and project settings.
    
    ONLY include:
    - resource{} blocks
    - data{} blocks  
    - variable{} blocks
    - output{} blocks
    - locals{} blocks
    
    Always provide complete, valid Terraform HCL code as the content parameter.
    """
    
    logger.info(f"iac_write called with user_id {user_id} and session_id {session_id}")
    
    # Validate that we have the required context parameters
    if not user_id:
        logger.error("iac_write: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "write"})
    if not session_id:
        logger.error("iac_write: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "write"})
    
    from utils.storage.storage import get_storage_manager

    try:
        # Clear terraform state if provider changed
        clear_terraform_state_if_provider_changed(user_id, session_id)
        
        # Dynamically resolve the resource ID and provider type
        # Pass the content so we can detect provider from Terraform resource prefixes
        # (handles typos like "sacaleway" -> LLM still generates correct "scaleway_" resources)
        resource_id, provider_type = _resolve_resource_id(user_id, terraform_content=content)
        terraform_dir = get_terraform_directory(user_id, session_id)
        
        # Always treat content as literal terraform content
        logger.info(f"Using custom terraform content for path: {path}")
        file_path = terraform_dir / path
        # Ensure parent directories exist
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tf_content = content
        
        # Write files via terminal pod for persistence
        from utils.terminal.terminal_run import terminal_run
        
        # Write main manifest
        encoded_content = base64.b64encode(tf_content.encode()).decode()
        write_cmd = f"mkdir -p {terraform_dir} && echo '{encoded_content}' | base64 -d > {file_path}"
        result = terminal_run(write_cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to write {path}: {result.stderr}")
        
        # Only write provider.tf if user hasn't provided terraform/provider blocks
        # Use regex for more robust detection (handles spacing, newlines, comments)
        has_terraform_block = bool(re.search(r'^\s*terraform\s*\{', tf_content, re.MULTILINE))
        has_provider_block = bool(re.search(r'^\s*provider\s+"', tf_content, re.MULTILINE))
        
        if not (has_terraform_block or has_provider_block):
            # Write provider configuration
            provider_file = terraform_dir / "provider.tf"
            if provider_type == "azure":
                provider_config = generate_azure_provider_config(resource_id)
            elif provider_type == "aws":
                provider_config = generate_aws_provider_config(resource_id)
            elif provider_type == "ovh":
                provider_config = generate_ovh_provider_config(resource_id)
            elif provider_type == "scaleway":
                provider_config = generate_scaleway_provider_config(resource_id)
            else:
                provider_config = generate_gcp_provider_config(resource_id)
            
            encoded_provider = base64.b64encode(provider_config.encode()).decode()
            provider_cmd = f"echo '{encoded_provider}' | base64 -d > {provider_file}"
            result = terminal_run(provider_cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to write provider.tf: {result.stderr}")
            
            # Upload provider.tf to storage for persistence
            try:
                storage = get_storage_manager(user_id=user_id)
                provider_storage_path = f"{session_id}/terraform_dir/provider.tf"

                # Read provider.tf from terminal pod and upload
                read_provider_cmd = f"cat {provider_file}"
                result_read = terminal_run(read_provider_cmd, shell=True, capture_output=True, text=True, timeout=30)
                if result_read.returncode == 0:
                    file_like = io.BytesIO(result_read.stdout.encode())
                    file_like.name = "provider.tf"
                    file_like.content_type = 'application/octet-stream'
                    storage.upload_file(file_like, provider_storage_path, user_id=user_id)
                    logger.info(f"Uploaded provider.tf to storage: {provider_storage_path}")
            except Exception as e:
                logger.warning(f"Failed to upload provider.tf to storage: {e}")
        else:
            logger.info(f"User provided terraform/provider blocks, skipping provider.tf generation")
            # DELETE any existing provider.tf to avoid "Duplicate required providers" conflict
            provider_file = terraform_dir / "provider.tf"
            delete_cmd = f"rm -f {provider_file}"
            terminal_run(delete_cmd, shell=True, capture_output=True, text=True, timeout=10)
            logger.info(f"Deleted existing provider.tf to avoid conflicts")

        # Upload to storage for persistence (files written to terminal pod, read back for storage)
        storage_path = None
        try:
            storage = get_storage_manager(user_id=user_id)
            storage_blob_path = f"{session_id}/terraform_dir/{file_path.name}"

            # Read file from terminal pod and upload
            read_cmd = f"cat {file_path}"
            result_read = terminal_run(read_cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result_read.returncode == 0:
                file_like = io.BytesIO(result_read.stdout.encode())
                file_like.name = file_path.name
                file_like.content_type = 'application/octet-stream'
                storage_path = storage.upload_file(file_like, storage_blob_path, user_id=user_id)
                logger.info(f"Uploaded IaC file to storage: {storage_path}")
        except Exception as e:
            logger.warning(f"Failed to upload IaC file to storage: {e}")

        result = {
            "status": "success",
            "action": "write",
            "message": f"IaC manifest written to {file_path}",
            "path": str(file_path),
            "terraform_directory": str(terraform_dir),
            "resource_id": resource_id,
            "provider_type": provider_type,
            "content_type": "custom",
            "chat_output": tf_content,
            "storage_path": storage_path
        }
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Error in iac_write: {e}")
        return json.dumps({"error": f"Failed to write IaC manifest: {str(e)}", "action": "write"})
