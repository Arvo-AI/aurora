"""
Secure logging utilities to prevent credential exposure in logs.
"""

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Common credential field patterns to censor
CREDENTIAL_PATTERNS = [
    'password', 'secret', 'key', 'token', 'credential', 'auth',
    'private_key', 'client_secret', 'access_key', 'secret_key',
    'api_key', 'webhook_secret', 'session_token', 'refresh_token'
]

def mask_credential_value(value: str, show_prefix: int = 4) -> str:
    """
    Mask a credential value, showing only a prefix for identification.
    
    Args:
        value: The credential value to mask
        show_prefix: Number of characters to show at the beginning
        
    Returns:
        Masked string like "sk_te***MASKED***"
    """
    if not value or len(value) <= show_prefix:
        return "***MASKED***"
    
    return f"{value[:show_prefix]}***MASKED***"

def is_credential_field(field_name: str) -> bool:
    """
    Check if a field name appears to contain credential data.
    
    Args:
        field_name: Field name to check
        
    Returns:
        True if field appears to contain credentials
    """
    field_lower = field_name.lower()
    return any(pattern in field_lower for pattern in CREDENTIAL_PATTERNS)

def safe_log_dict(data: Dict[str, Any], logger_func: callable, message: str = "Data") -> None:
    """
    Safely log a dictionary by masking credential fields.
    
    Args:
        data: Dictionary to log
        logger_func: Logger function to use (e.g., logger.info)
        message: Prefix message for the log
    """
    if not data:
        logger_func(f"{message}: (empty)")
        return
    
    safe_data = {}
    for key, value in data.items():
        if is_credential_field(key):
            if isinstance(value, str):
                safe_data[key] = mask_credential_value(value)
            else:
                safe_data[key] = "***MASKED***"
        else:
            safe_data[key] = value
    
    logger_func(f"{message}: {safe_data}")

def safe_log_credential_keys(data: Dict[str, Any], logger_func: callable, message: str = "Available credential keys") -> None:
    """
    Log only the keys of a credential dictionary, not values.
    
    Args:
        data: Dictionary to log keys for
        logger_func: Logger function to use
        message: Message to log
    """
    if not data:
        logger_func(f"{message}: (none)")
        return
    
    keys = list(data.keys())
    logger_func(f"{message}: {keys}")

def censor_aws_credentials(creds: Dict[str, Any]) -> Dict[str, Any]:
    """
    Censor AWS credentials for safe logging.
    
    Args:
        creds: AWS credentials dictionary
        
    Returns:
        Censored credentials dictionary
    """
    safe_creds = creds.copy()
    
    if 'access_key' in safe_creds:
        safe_creds['access_key'] = mask_credential_value(safe_creds['access_key'], 4)
    if 'aws_access_key_id' in safe_creds:
        safe_creds['aws_access_key_id'] = mask_credential_value(safe_creds['aws_access_key_id'], 4)
    if 'secret_key' in safe_creds:
        safe_creds['secret_key'] = "***MASKED***"
    if 'aws_secret_access_key' in safe_creds:
        safe_creds['aws_secret_access_key'] = "***MASKED***"
    if 'session_token' in safe_creds:
        safe_creds['session_token'] = "***MASKED***" if safe_creds['session_token'] else None
    
    return safe_creds

def censor_azure_credentials(creds: Dict[str, Any]) -> Dict[str, Any]:
    """
    Censor Azure credentials for safe logging.
    
    Args:
        creds: Azure credentials dictionary
        
    Returns:
        Censored credentials dictionary
    """
    safe_creds = creds.copy()
    
    if 'client_secret' in safe_creds:
        safe_creds['client_secret'] = "***MASKED***"
    if 'access_token' in safe_creds:
        safe_creds['access_token'] = mask_credential_value(safe_creds['access_token'], 10)
    
    return safe_creds

def censor_gcp_credentials(creds: Dict[str, Any]) -> Dict[str, Any]:
    """
    Censor GCP credentials for safe logging.
    
    Args:
        creds: GCP credentials dictionary
        
    Returns:
        Censored credentials dictionary
    """
    safe_creds = creds.copy()
    
    if 'private_key' in safe_creds:
        safe_creds['private_key'] = "***MASKED***"
    if 'access_token' in safe_creds:
        safe_creds['access_token'] = mask_credential_value(safe_creds['access_token'], 10)
    if 'refresh_token' in safe_creds:
        safe_creds['refresh_token'] = mask_credential_value(safe_creds['refresh_token'], 10)
    
    return safe_creds

# Convenience functions for specific providers
def safe_log_aws_creds(creds: Dict[str, Any], logger_func: callable, message: str = "AWS credentials") -> None:
    """Log AWS credentials safely."""
    censored = censor_aws_credentials(creds)
    logger_func(f"{message}: {censored}")

def safe_log_azure_creds(creds: Dict[str, Any], logger_func: callable, message: str = "Azure credentials") -> None:
    """Log Azure credentials safely."""
    censored = censor_azure_credentials(creds)
    logger_func(f"{message}: {censored}")

def safe_log_gcp_creds(creds: Dict[str, Any], logger_func: callable, message: str = "GCP credentials") -> None:
    """Log GCP credentials safely."""
    censored = censor_gcp_credentials(creds)
    logger_func(f"{message}: {censored}")
