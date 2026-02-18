"""
AWS Onboarding Routes
Manual AWS onboarding via IAM role ARN with STS AssumeRole.
"""
import logging
import os
from flask import Blueprint, request, jsonify
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.workspace.workspace_utils import (
    get_or_create_workspace,
    get_workspace_by_id,
    update_workspace_aws_role,
    is_workspace_aws_configured,
    get_workspace_aws_status
)

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint("aws_onboarding_bp", __name__)


def get_authenticated_user_id():
    """Get authenticated user ID from X-User-ID header."""
    return get_user_id_from_request()


@onboarding_bp.route('/aws/env/check', methods=['GET', 'OPTIONS'])
def check_aws_environment():
    """
    Check if Aurora has AWS environment configured (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY).
    
    Returns:
        {
            "configured": bool,
            "hasAccessKey": bool,
            "hasSecretKey": bool,
            "accountId": str | null  # Only if credentials are configured and valid
        }
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
        secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')
        
        has_access_key = bool(access_key_id)
        has_secret_key = bool(secret_access_key)
        configured = has_access_key and has_secret_key
        
        account_id = None
        if configured:
            # Try to get account ID using the credentials
            try:
                from utils.aws.aws_sts_client import get_aurora_account_id
                account_id = get_aurora_account_id()
            except Exception as e:
                logger.debug(f"Could not get account ID even though credentials are set: {e}")
        
        return jsonify({
            "configured": configured,
            "hasAccessKey": has_access_key,
            "hasSecretKey": has_secret_key,
            "accountId": account_id
        })
        
    except Exception as e:
        logger.error(f"Failed to check AWS environment: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/links', methods=['GET', 'OPTIONS'])
def get_aws_onboarding_links(workspace_id):
    """
    Get AWS onboarding information for a workspace (external ID and status).
    
    Returns basic information needed for manual role setup.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        
        # Get workspace
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404
        
        # Check ownership
        if workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        # Auto-detect Aurora's account ID
        from utils.aws.aws_sts_client import get_aurora_account_id
        aurora_account_id = get_aurora_account_id()
        
        # Prepare response
        response_data = {
            "workspaceId": workspace_id,
            "externalId": workspace['aws_external_id'],
            "status": get_workspace_aws_status(workspace)
        }

        # Include Aurora account ID if available
        if aurora_account_id:
            response_data["auroraAccountId"] = aurora_account_id

        # Include roleArn from user_connections (single source of truth)
        from utils.db.connection_utils import get_user_aws_connection
        aws_conn = get_user_aws_connection(user_id)
        if aws_conn and aws_conn.get('role_arn'):
            response_data["roleArn"] = aws_conn['role_arn']
        
        logger.info(f"Retrieved AWS onboarding info for workspace {workspace_id}")
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Failed to get AWS onboarding info for workspace {workspace_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/role', methods=['POST', 'OPTIONS'])
def set_aws_role(workspace_id):
    """
    Manually set the AWS role ARN for a workspace.
    
    Expected payload:
    {
        "roleArn": "arn:aws:iam::123456789012:role/AuroraRole",
        "readOnlyRoleArn": "arn:aws:iam::123456789012:role/AuroraReadOnly"  // optional
    }
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        
        # Get workspace and verify ownership
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404
        
        if workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        role_arn = data.get('roleArn')
        if not role_arn:
            return jsonify({"error": "roleArn is required"}), 400
        
        # Basic ARN validation
        if not role_arn.startswith('arn:aws:iam::'):
            return jsonify({"error": "Invalid role ARN format"}), 400

        read_only_role_arn = data.get('readOnlyRoleArn') or data.get('read_only_role_arn')

        from utils.aws.aws_sts_client import assume_workspace_role, get_aurora_account_id

        # Auto-detect Aurora's account ID
        aurora_account_id = get_aurora_account_id()
        if not aurora_account_id:
            logger.error("Could not determine Aurora's AWS account ID. Ensure Aurora has AWS credentials configured.")
            return jsonify({
                "error": "Server configuration error: Unable to determine Aurora's AWS account ID. Please ensure Aurora has AWS credentials configured."
            }), 500

        # Validate that Aurora can actually assume the role using STS
        try:
            # We only need to know if the call succeeds; short session (15 min) is enough
            assume_workspace_role(role_arn, workspace['aws_external_id'], workspace_id, duration_seconds=900)
        except Exception as e:
            logger.warning(f"Role validation failed for workspace {workspace_id} using {role_arn}: {e}")
            
            # Extract account ID from role ARN for better error messaging
            try:
                account_id = role_arn.split(':')[4]
            except (IndexError, AttributeError):
                account_id = "your AWS account"
            
            error_message = (
                f"Aurora cannot assume this role. Please verify:\n\n"
                f"1. The role exists in {account_id}\n"
                f"2. The role's trust policy includes Aurora as a trusted entity:\n"
                f"   - Principal: arn:aws:iam::{aurora_account_id}:root\n"
                f"   - ExternalId: {workspace['aws_external_id']}\n\n"
                f"3. The role has the necessary permissions\n\n"
                f"Check the IAM console and ensure the trust relationship is configured correctly."
            )
            
            return jsonify({
                "error": "Role assumption failed",
                "message": error_message,
                "details": {
                    "role_arn": role_arn,
                    "external_id": workspace['aws_external_id'],
                    "account_id": account_id
                }
            }), 400

        if read_only_role_arn:
            if not read_only_role_arn.startswith('arn:aws:iam::'):
                return jsonify({"error": "Invalid readOnlyRoleArn format"}), 400
            try:
                assume_workspace_role(read_only_role_arn, workspace['aws_external_id'], workspace_id, duration_seconds=900)
            except Exception as read_only_error:
                logger.warning(
                    "Read-only role validation failed for workspace %s using %s: %s",
                    workspace_id,
                    read_only_role_arn,
                    read_only_error,
                )
                return jsonify({
                    "error": "Read-only role assumption failed",
                    "message": str(read_only_error),
                    "details": {
                        "role_arn": read_only_role_arn,
                        "external_id": workspace['aws_external_id'],
                    }
                }), 400

        # Update user_connections table (single source of truth)
        # This also updates workspace table for compatibility, but connection state comes from user_connections
        update_workspace_aws_role(
            workspace_id,
            role_arn,
            read_only_role_arn=read_only_role_arn,
        )

        logger.info(
            "Set AWS role for workspace %s: %s (read-only: %s) - saved to user_connections",
            workspace_id,
            role_arn,
            read_only_role_arn,
        )
        return jsonify({"ok": True})
        
    except Exception as e:
        logger.error(f"Failed to set AWS role for workspace {workspace_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/status', methods=['GET', 'OPTIONS'])
def get_aws_onboarding_status(workspace_id):
    """
    Get current AWS onboarding status for a workspace.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        
        # Get workspace and verify ownership
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404
        
        if workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        status = get_workspace_aws_status(workspace)
        
        # Get AWS connection from user_connections (single source of truth)
        from utils.db.connection_utils import get_user_aws_connection
        aws_conn = get_user_aws_connection(user_id)
        
        response_data = {
            "status": status,
            "isConfigured": is_workspace_aws_configured(workspace),
            "externalId": workspace.get('aws_external_id'),  # Still from workspace (STS needs it)
            "roleArn": aws_conn.get('role_arn') if aws_conn else None,
            "readOnlyRoleArn": aws_conn.get('read_only_role_arn') if aws_conn else None,
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Failed to get AWS status for workspace {workspace_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500



@onboarding_bp.route('/users/<user_id>/workspaces', methods=['GET', 'POST', 'OPTIONS'])
def manage_user_workspaces(user_id):
    """
    Get user workspaces (GET) or create new workspace (POST).
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        authenticated_user_id = get_authenticated_user_id()
        if not authenticated_user_id:
            return jsonify({"error": "Unauthorized"}), 401
        
        # Check if user can access this user's workspaces
        if authenticated_user_id != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        if request.method == 'GET':
            # Return user workspaces (for now, auto-create default)
            workspace = get_or_create_workspace(user_id, "default")
            return jsonify({"workspaces": [workspace]})
        
        elif request.method == 'POST':
            # Create new workspace
            data = request.get_json() or {}
            workspace_name = data.get('name', 'default')
            
            workspace = get_or_create_workspace(user_id, workspace_name)
            return jsonify(workspace)
        
    except Exception as e:
        logger.error(f"Failed to manage workspaces for user {user_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500



@onboarding_bp.route('/workspaces/<workspace_id>/aws/cleanup', methods=['POST', 'OPTIONS'])
def workspace_cleanup(workspace_id):
    """Disconnect AWS connection by removing it from user_connections (single source of truth).
    
    This endpoint now properly disconnects AWS by removing the connection from user_connections.
    Users must manually remove IAM roles and other AWS resources in their AWS console.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        # Verify workspace ownership
        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        # Disconnect AWS using the proper disconnect endpoint (single source of truth)
        from utils.db.connection_utils import (
            get_user_aws_connection,
            delete_connection_secret,
        )
        
        aws_conn = get_user_aws_connection(user_id)
        if not aws_conn:
            return jsonify({
                "success": True, 
                "message": "AWS connection already disconnected."
            })

        # Delete connection from user_connections (single source of truth)
        account_id = aws_conn.get('account_id')
        if account_id:
            success = delete_connection_secret(user_id, "aws", account_id)
            if not success:
                logger.error("Failed to delete AWS connection for user %s account %s", user_id, account_id)
                return jsonify({"error": "Failed to disconnect AWS connection"}), 500
        
        # Clean up workspace discovery fields (role info is only in user_connections now)
        try:
            from utils.db.connection_pool import db_pool
            with db_pool.get_admin_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """UPDATE workspaces SET aws_discovery_summary = NULL,
                       aws_discovery_artifact_bucket = NULL,
                       aws_discovery_artifact_key = NULL,
                       updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s""",
                    (workspace_id,),
                )
                conn.commit()
        except Exception as db_exc:
            logger.warning("Failed to clear workspace discovery fields for %s: %s", workspace_id, db_exc)
            # Don't fail the request - connection is already removed from user_connections

        message = (
            "Aurora has disconnected AWS. "
            "Please manually remove any IAM roles in your AWS console if you no longer need them. "
            "You can now restart the onboarding flow from scratch."
        )

        return jsonify({"success": True, "message": message})

    except Exception as e:
        logger.error(f"Failed workspace cleanup for {workspace_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500
