"""
Cloud authentication utilities for Azure and GCP.
Consolidates authentication functions used by both chat and connectors.
"""

import logging
import os
import time
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from utils.cloud.cloud_utils import get_mode_from_context

logger = logging.getLogger(__name__)


READ_ONLY_MODE = "ask"


def _normalize_mode(mode: Optional[str]) -> str:
    normalized = (mode or "").strip().lower()
    return normalized if normalized else "agent"


# =============================================================================
# AZURE AUTHENTICATION FUNCTIONS
# =============================================================================

def _resolve_azure_credentials(token_data: Dict[str, Any], normalized_mode: str, user_id: str) -> Dict[str, Any]:
    base = {
        "tenant_id": token_data.get("tenant_id"),
        "client_id": token_data.get("client_id"),
        "client_secret": token_data.get("client_secret"),
        "subscription_id": token_data.get("subscription_id"),
    }

    if normalized_mode == READ_ONLY_MODE:
        read_only_block = token_data.get("read_only") or {}
        if isinstance(read_only_block, dict):
            candidate = {
                "tenant_id": read_only_block.get("tenant_id") or base["tenant_id"],
                "client_id": read_only_block.get("client_id"),
                "client_secret": read_only_block.get("client_secret"),
                "subscription_id": read_only_block.get("subscription_id") or base.get("subscription_id"),
            }
            if candidate["client_id"] and candidate["client_secret"]:
                return candidate
            logger.warning(
                "Azure read-only credentials incomplete for user %s; using full-access identity",
                user_id,
            )

    return base


def generate_azure_access_token(user_id: str, subscription_id: Optional[str] = None, force_new: bool = False,
                                mode: Optional[str] = None) -> Dict[str, Any]:
    """Generate Azure access token for tools, similar to GCP's generate_contextual_access_token.
    
    Args:
        user_id: The user ID for authentication
        subscription_id: Optional subscription ID (will use stored subscription if not provided)
        force_new: If True, skip checking existing token and generate new one
    
    Returns:
        dict: Contains access_token, subscription_id, tenant_id, expires_on
        
    Raises:
        ValueError: If authentication fails or no credentials found
    """
    normalized_mode = _normalize_mode(mode)

    # Check for existing valid token first (unless force_new is True)
    if not force_new:
        try:
            from utils.auth.token_management import get_token_data
            existing_token = get_token_data(user_id, "azure")
            if existing_token and existing_token.get('access_token'):
                token_mode = _normalize_mode(existing_token.get('mode'))
                if token_mode != normalized_mode:
                    logger.info(
                        "Existing Azure token for user %s has mode %s; requested mode %s. Generating new token.",
                        user_id,
                        token_mode,
                        normalized_mode,
                    )
                else:
                    # Check if token is still valid (with 5-minute buffer)
                    expires_on = existing_token.get('expires_on')
                    if expires_on:
                        try:
                            # Azure expires_on is typically a timestamp
                            if isinstance(expires_on, (int, float)):
                                expire_time = datetime.fromtimestamp(expires_on)
                            elif isinstance(expires_on, str):
                                # Try parsing as ISO format or timestamp
                                try:
                                    expire_time = datetime.fromisoformat(expires_on.replace('Z', '+00:00'))
                                except Exception:
                                    expire_time = datetime.fromtimestamp(float(expires_on))
                            else:
                                raise ValueError(f"Unknown expires_on format: {type(expires_on)}")
                            
                            current_time = datetime.now(expire_time.tzinfo) if expire_time.tzinfo else datetime.now()
                            buffer_seconds = 300  # 5-minute buffer
                            remaining_seconds = (expire_time - current_time).total_seconds()
                            
                            if current_time < (expire_time - timedelta(seconds=buffer_seconds)):
                                logger.info(
                                    "Reusing existing Azure token for user %s (mode=%s, valid for %.0fs)",
                                    user_id,
                                    normalized_mode,
                                    remaining_seconds,
                                )
                                
                                return {
                                    'access_token': existing_token['access_token'],
                                    'subscription_id': existing_token.get('subscription_id') or subscription_id,
                                    'tenant_id': existing_token.get('tenant_id'),
                                    'client_id': existing_token.get('client_id'),  # Include for CLI auth
                                    'client_secret': existing_token.get('client_secret'),  # Include for CLI auth
                                    'expires_on': existing_token.get('expires_on'),
                                    'auth_method': existing_token.get('auth_method', 'service_principal'),
                                    'mode': normalized_mode,
                                }
                            else:
                                logger.info(
                                    "Existing Azure token for user %s expires soon (%.0fs), generating new one",
                                    user_id,
                                    remaining_seconds,
                                )
                        except Exception as parse_error:
                            logger.warning(f"Could not parse Azure token expiry time '{expires_on}': {parse_error}")
                    else:
                        logger.info(f"Existing Azure token for user {user_id} has no expiry time, generating new one")
            else:
                logger.info(f"No existing Azure token found for user {user_id}")
        except Exception as check_error:
            logger.warning(f"Failed to check existing Azure token for user {user_id}: {check_error}")
            # Continue to generate new token
    
    try:
        # Get Azure credentials from database (stored securely in Vault)
        from utils.auth.token_management import get_token_data
        token_data = get_token_data(user_id, "azure")
        if not token_data:
            raise ValueError(f"No Azure credentials found for user {user_id}")
        
        # SECURITY: Log available credential keys only, not values
        from utils.logging.secure_logging import safe_log_credential_keys
        safe_log_credential_keys(token_data, logger.info, f"Retrieved Azure credentials for user {user_id}")
        
        credential_source = _resolve_azure_credentials(token_data, normalized_mode, user_id)

        # Extract credentials
        tenant_id = credential_source.get("tenant_id")
        client_id = credential_source.get("client_id")
        client_secret = credential_source.get("client_secret")
        stored_subscription_id = credential_source.get("subscription_id")
        
        if not all([tenant_id, client_id, client_secret]):
            raise ValueError("Incomplete Azure credentials - missing tenant_id, client_id, or client_secret")
        
        # Use provided subscription_id or fall back to stored one
        target_subscription_id = subscription_id or stored_subscription_id
        if not target_subscription_id:
            raise ValueError("No subscription ID provided and no stored subscription found")
        
        logger.info(f"Generating new Azure access token for user {user_id}")
        
        # Create Azure credential object
        from azure.identity import ClientSecretCredential
        credential = ClientSecretCredential(
            tenant_id=str(tenant_id),
            client_id=str(client_id),
            client_secret=str(client_secret)
        )
        
        # Get management token
        token = credential.get_token("https://management.azure.com/.default")
        if not token:
            raise ValueError("Failed to get Azure management token")
        
        # Store the token for reuse (preserve existing metadata such as read-only config)
        azure_token_data = dict(token_data)
        azure_token_data.update({
            "access_token": token.token,
            "subscription_id": target_subscription_id,
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "expires_on": token.expires_on,
            "auth_method": "service_principal",
            "generated_at": time.time(),
            "mode": normalized_mode,
        })
        
        # Store token data for reuse
        try:
            from utils.auth.token_management import store_tokens_in_db
            store_tokens_in_db(user_id, azure_token_data, "azure")
            logger.info(f"Stored new Azure token in database for user {user_id}")
        except Exception as store_error:
            logger.warning(f"Failed to store Azure token in database: {store_error}")
            # Continue anyway - token generation succeeded
        
        # SECURITY: Mask subscription ID in logs
        from utils.logging.secure_logging import mask_credential_value
        masked_subscription = mask_credential_value(target_subscription_id, 8)
        logger.info(f"Successfully generated Azure access token for user {user_id}, subscription {masked_subscription}")
        
        # Return complete credentials including client_id and client_secret for CLI auth
        return {
            "access_token": token.token,
            "subscription_id": target_subscription_id,
            "tenant_id": tenant_id,
            "client_id": client_id,  # Required for Azure CLI auth
            "client_secret": client_secret,  # Required for Azure CLI auth
            "expires_on": token.expires_on,
            "auth_method": "service_principal",
            "mode": normalized_mode,
        }
        
    except Exception as e:
        logger.error(f"Failed to generate Azure access token for user {user_id}: {e}")
        raise ValueError(f"Azure authentication failed: {e}")


def get_azure_credentials_for_tools(user_id: str) -> Dict[str, Any]:
    """Get Azure credentials formatted for tool usage.
    
    Args:
        user_id: The user ID for authentication
        
    Returns:
        dict: Formatted credentials for tools
    """
    try:
        token_info = generate_azure_access_token(user_id)
        
        return {
            "access_token": token_info["access_token"],
            "subscription_id": token_info["subscription_id"],
            "tenant_id": token_info["tenant_id"],
            "expires_on": token_info["expires_on"],
            "auth_method": token_info["auth_method"]
        }
        
    except Exception as e:
        logger.error(f"Failed to get Azure credentials for tools: {e}")
        raise


def verify_azure_access(user_id: str, subscription_id: Optional[str] = None) -> bool:
    """Verify that Azure credentials are valid and accessible.
    
    Args:
        user_id: The user ID for authentication
        subscription_id: Optional subscription ID to verify access to
        
    Returns:
        bool: True if credentials are valid and accessible
    """
    try:
        token_info = generate_azure_access_token(user_id, subscription_id)
        
        # Make a simple API call to verify access
        import requests
        headers = {"Authorization": f"Bearer {token_info['access_token']}"}
        test_subscription_id = token_info["subscription_id"]
        
        response = requests.get(
            f"https://management.azure.com/subscriptions/{test_subscription_id}?api-version=2020-01-01",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info(f"Azure access verified for user {user_id}, subscription {test_subscription_id}")
            return True
        else:
            logger.warning(f"Azure access verification failed: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"Azure access verification failed for user {user_id}: {e}")
        return False


# =============================================================================
# GCP AUTHENTICATION FUNCTIONS
# =============================================================================

def generate_gcp_access_token(
    user_id: str,
    scopes: List[str] = None,
    lifetime: int = 3600,
    selected_project_id: str = None,
    mode: Optional[str] = None,
) -> Dict:
    """Generate access token via GCP service account impersonation."""
    from connectors.gcp_connector.auth.service_accounts import generate_sa_access_token

    return generate_sa_access_token(user_id, scopes, lifetime, selected_project_id, mode=mode)


def get_provider_preference_from_context() -> Optional[str]:
    """Get the provider preference from thread context if available."""
    try:
        # from chat.backend.agent.tools.cloud_tools import get_provider_preference, get_user_context
        from utils.cloud.cloud_utils import get_provider_preference, get_user_context
        preference = get_provider_preference()
        
        # Handle new list format - return first provider if it's a list
        if isinstance(preference, list) and len(preference) > 0:
            return preference[0]
        elif isinstance(preference, str):
            return preference
        else:
            # No preference found - return None to respect explicit user selection
            return None
    except ImportError:
        logger.debug("cloud_tools not available, provider preference not set")
        return None
    except Exception as e:
        logger.debug(f"Error getting provider preference: {e}")
        return None


def generate_contextual_access_token(user_id: str,
                                   scopes: List[str] = None,
                                   lifetime: int = 3600,
                                   selected_project_id: str = None,
                                   override_provider: str = None,
                                   mode: Optional[str] = None) -> Dict:
    """Generate a GCP access token with context-aware selection."""
    # Determine provider preference
    provider = override_provider or get_provider_preference_from_context()
    if mode is None:
        mode = get_mode_from_context()
    
    if not provider:
        # No automatic fallback - the caller must ensure a provider is selected
        # Raise an error to prevent executing commands without explicit provider selection
        logger.warning(f"No provider preference found for user {user_id}. User must select a provider in the dropdown.")
        # Return a minimal error-indicating token response instead of failing hard
        return {
            "error": "No cloud provider connected. Please connect a provider first.",
            "requires_connection": True
        }
    
    if provider:
        provider = provider.strip().lower()

    if provider and provider != "gcp":
        logger.info("Provider preference '%s' not supported for GCP auth; defaulting to GCP", provider)
        provider = "gcp"

    return generate_gcp_access_token(
        user_id=user_id,
        scopes=scopes,
        lifetime=lifetime,
        selected_project_id=selected_project_id,
        mode=mode,
    )
