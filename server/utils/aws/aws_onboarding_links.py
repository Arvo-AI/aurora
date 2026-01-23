"""
AWS Onboarding Utilities
Simple utilities for manual AWS onboarding via IAM role ARN.
"""
import logging
import re
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def validate_external_id(external_id: str) -> bool:
    """
    Validate that external_id follows UUID v4 format.
    
    Args:
        external_id: External ID to validate
        
    Returns:
        True if valid UUID v4 format
    """
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    return bool(re.match(uuid_pattern, external_id, re.IGNORECASE))


def generate_external_id() -> str:
    """
    Generate a new UUID v4 external ID.
    
    Returns:
        UUID v4 string
    """
    return str(uuid.uuid4())
