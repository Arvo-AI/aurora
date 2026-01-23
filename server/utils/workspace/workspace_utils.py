"""
Workspace Management Utilities
Handles workspace creation, updates, and AWS onboarding state management.
"""
import logging
import uuid
import json
from typing import Dict, Optional, Any
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


def get_or_create_workspace(user_id: str, workspace_name: str = "default") -> Dict[str, Any]:
    """
    Get existing workspace or create a new one for a user.
    
    Args:
        user_id: User identifier
        workspace_name: Workspace name (defaults to "default")
        
    Returns:
        Dictionary with workspace data
        
    Raises:
        Exception: Database or validation errors
    """
    if not user_id:
        raise ValueError("user_id is required")
    
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            
            # Try to get existing workspace
            cursor.execute(
                "SELECT id, user_id, name, aws_external_id, aws_discovery_artifact_bucket, "
                "aws_discovery_artifact_key, aws_discovery_summary, created_at, updated_at "
                "FROM workspaces WHERE user_id = %s AND name = %s",
                (user_id, workspace_name)
            )
            
            result = cursor.fetchone()
            if result:
                # Deserialize JSON data from database
                discovery_summary = result[6]
                if discovery_summary and isinstance(discovery_summary, str):
                    try:
                        discovery_summary = json.loads(discovery_summary)
                    except (json.JSONDecodeError, TypeError):
                        discovery_summary = None
                
                workspace = {
                    'id': result[0],
                    'user_id': result[1],
                    'name': result[2],
                    'aws_external_id': result[3],
                    'aws_discovery_artifact_bucket': result[4],
                    'aws_discovery_artifact_key': result[5],
                    'aws_discovery_summary': discovery_summary,
                    'created_at': result[7],
                    'updated_at': result[8]
                }
                logger.info(f"Retrieved existing workspace {workspace['id']} for user {user_id}")
                return workspace
            
            # Create new workspace
            workspace_id = str(uuid.uuid4())
            external_id = str(uuid.uuid4())  # Generate ExternalId immediately
            
            cursor.execute(
                "INSERT INTO workspaces (id, user_id, name, aws_external_id) "
                "VALUES (%s, %s, %s, %s)",
                (workspace_id, user_id, workspace_name, external_id)
            )
            
            conn.commit()
            
            # Return the created workspace
            workspace = {
                'id': workspace_id,
                'user_id': user_id,
                'name': workspace_name,
                'aws_external_id': external_id,
                'aws_discovery_artifact_bucket': None,
                'aws_discovery_artifact_key': None,
                'aws_discovery_summary': None,
                'created_at': None,  # Will be set by database
                'updated_at': None
            }
            
            logger.info(f"Created new workspace {workspace_id} for user {user_id} with external_id {external_id}")
            return workspace
            
    except Exception as e:
        logger.error(f"Failed to get/create workspace for user {user_id}: {e}")
        raise


def update_workspace_aws_role(
    workspace_id: str,
    role_arn: str,
    artifact_bucket: Optional[str] = None,
    artifact_key: Optional[str] = None,
    read_only_role_arn: Optional[str] = None,
) -> None:
    """
    Save AWS connection to user_connections (single source of truth).
    Workspace table is only used for aws_external_id (needed for STS).
    
    Args:
        workspace_id: Workspace identifier
        role_arn: IAM role ARN for Aurora to assume
        artifact_bucket: Optional S3 bucket (legacy, not used in manual flow)
        artifact_key: Optional S3 key (legacy, not used in manual flow)
        read_only_role_arn: Optional read-only IAM role ARN
        
    Raises:
        Exception: Database errors
    """
    if not workspace_id or not role_arn:
        raise ValueError("workspace_id and role_arn are required")
    
    try:
        from utils.db.connection_utils import (
            save_connection_metadata,
            extract_account_id_from_arn,
        )

        # Get user_id from workspace
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")
        
        user_id = workspace.get('user_id')
        if not user_id:
            raise ValueError(f"Workspace {workspace_id} has no user_id")

        # Save to user_connections (single source of truth)
        account_id = extract_account_id_from_arn(role_arn) or "unknown"
        save_connection_metadata(
            user_id=user_id,
            provider="aws",
            account_id=account_id,
            role_arn=role_arn,
            read_only_role_arn=read_only_role_arn,
            connection_method="sts_assume_role",
            status="active",
        )
        logger.info("Saved AWS connection to user_connections for user %s (account: %s)", user_id, account_id)

    except Exception as e:
        logger.error(f"Failed to save AWS connection for workspace {workspace_id}: {e}")
        raise


def get_workspace_by_id(workspace_id: str) -> Optional[Dict[str, Any]]:
    """
    Get workspace by ID.
    
    Args:
        workspace_id: Workspace identifier
        
    Returns:
        Workspace dictionary or None if not found
    """
    if not workspace_id:
        return None
    
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT id, user_id, name, aws_external_id, aws_discovery_artifact_bucket, "
                "aws_discovery_artifact_key, aws_discovery_summary, created_at, updated_at "
                "FROM workspaces WHERE id = %s",
                (workspace_id,)
            )
            
            result = cursor.fetchone()
            if not result:
                return None
            
            # Deserialize JSON data from database
            discovery_summary = result[6]
            if discovery_summary and isinstance(discovery_summary, str):
                try:
                    discovery_summary = json.loads(discovery_summary)
                except (json.JSONDecodeError, TypeError):
                    discovery_summary = None
            
            return {
                'id': result[0],
                'user_id': result[1],
                'name': result[2],
                'aws_external_id': result[3],
                'aws_discovery_artifact_bucket': result[4],
                'aws_discovery_artifact_key': result[5],
                'aws_discovery_summary': discovery_summary,
                'created_at': result[7],
                'updated_at': result[8]
            }
            
    except Exception as e:
        logger.error(f"Failed to get workspace {workspace_id}: {e}")
        return None


def get_user_workspaces(user_id: str) -> list:
    """
    Get all workspaces for a user.
    
    Args:
        user_id: User identifier
        
    Returns:
        List of workspace dictionaries
    """
    if not user_id:
        return []
    
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT id, user_id, name, aws_external_id, aws_discovery_artifact_bucket, "
                "aws_discovery_artifact_key, aws_discovery_summary, created_at, updated_at "
                "FROM workspaces WHERE user_id = %s ORDER BY created_at",
                (user_id,)
            )
            
            results = cursor.fetchall()
            workspaces = []
            
            for result in results:
                # Deserialize JSON data from database
                discovery_summary = result[6]
                if discovery_summary and isinstance(discovery_summary, str):
                    try:
                        discovery_summary = json.loads(discovery_summary)
                    except (json.JSONDecodeError, TypeError):
                        discovery_summary = None
                
                workspaces.append({
                    'id': result[0],
                    'user_id': result[1],
                    'name': result[2],
                    'aws_external_id': result[3],
                    'aws_discovery_artifact_bucket': result[4],
                    'aws_discovery_artifact_key': result[5],
                    'aws_discovery_summary': discovery_summary,
                    'created_at': result[7],
                    'updated_at': result[8]
                })
            
            return workspaces
            
    except Exception as e:
        logger.error(f"Failed to get workspaces for user {user_id}: {e}")
        return []


def is_workspace_aws_configured(workspace: Dict[str, Any]) -> bool:
    """
    Check if workspace has complete AWS configuration.
    
    Args:
        workspace: Workspace dictionary (must contain user_id)
        
    Returns:
        True if AWS configuration is complete (checks user_connections table)
    """
    user_id = workspace.get('user_id')
    if not user_id:
        return False
    
    # Single source of truth: check user_connections table
    from utils.db.connection_utils import get_user_aws_connection
    aws_conn = get_user_aws_connection(user_id)
    return aws_conn is not None and aws_conn.get('role_arn') is not None


def get_workspace_aws_status(workspace: Dict[str, Any]) -> str:
    """
    Get human-readable AWS onboarding status.
    
    Args:
        workspace: Workspace dictionary (must contain user_id)
        
    Returns:
        Status string: 'not_started' or 'fully_configured'
    """
    user_id = workspace.get('user_id')
    if not user_id:
        return 'not_started'
    
    # Single source of truth: check user_connections table
    from utils.db.connection_utils import get_user_aws_connection
    aws_conn = get_user_aws_connection(user_id)
    
    if aws_conn and aws_conn.get('role_arn'):
        return 'fully_configured'
    
    return 'not_started'
