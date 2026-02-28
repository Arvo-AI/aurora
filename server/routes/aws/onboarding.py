"""
AWS Onboarding Routes
Manual AWS onboarding via IAM role ARN with STS AssumeRole.
Supports single-account and multi-account (bulk) onboarding.
"""
import logging
import os
from flask import Blueprint, request, jsonify, Response
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
                    "message": "Could not assume the specified read-only role. Please verify the role ARN and trust policy.",
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


# ---------------------------------------------------------------------------
# Multi-account endpoints
# ---------------------------------------------------------------------------


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts', methods=['GET', 'OPTIONS'])
def list_aws_accounts(workspace_id):
    """Return all active AWS accounts connected to this workspace's owner."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        from utils.db.connection_utils import get_all_user_aws_connections
        accounts = get_all_user_aws_connections(user_id)
        return jsonify({"accounts": accounts})

    except Exception as e:
        logger.error("Failed to list AWS accounts for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts/bulk', methods=['POST', 'OPTIONS'])
def bulk_register_aws_accounts(workspace_id):
    """Register multiple AWS accounts at once.

    Expected payload::

        {
            "accounts": [
                {"accountId": "123456789012", "roleArn": "arn:aws:iam::123456789012:role/AuroraReadOnlyRole", "region": "us-east-1"},
                ...
            ]
        }

    Each account is validated independently via STS AssumeRole.
    Returns per-account success/failure so partially-successful bulk imports
    are surfaced clearly to the caller.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        data = request.get_json()
        if not data or not isinstance(data.get("accounts"), list):
            return jsonify({"error": "Payload must contain an 'accounts' array"}), 400

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.aws.aws_sts_client import assume_workspace_role
        from utils.db.connection_utils import save_connection_metadata, extract_account_id_from_arn

        results = []
        for entry in data["accounts"]:
            role_arn = entry.get("roleArn", "").strip()
            account_id = entry.get("accountId", "").strip()
            region = entry.get("region", "us-east-1").strip()

            if not role_arn or not account_id:
                results.append({"accountId": account_id, "success": False, "error": "roleArn and accountId are required"})
                continue

            if not role_arn.startswith("arn:aws:iam::"):
                results.append({"accountId": account_id, "success": False, "error": "Invalid role ARN format"})
                continue

            arn_account = extract_account_id_from_arn(role_arn)
            if arn_account and arn_account != account_id:
                results.append({"accountId": account_id, "success": False, "error": f"accountId does not match role ARN (ARN has {arn_account})"})
                continue

            try:
                assume_workspace_role(
                    role_arn=role_arn,
                    external_id=external_id,
                    workspace_id=workspace_id,
                    duration_seconds=900,
                    region=region,
                )
            except Exception as assume_err:
                logger.warning("Role assumption failed for account %s: %s", account_id, assume_err)
                results.append({"accountId": account_id, "success": False, "error": "Role assumption failed. Check the role ARN and trust policy."})
                continue

            saved = save_connection_metadata(
                user_id,
                "aws",
                account_id,
                role_arn=role_arn,
                connection_method="sts_assume_role",
                region=region,
                status="active",
            )
            if saved:
                results.append({"accountId": account_id, "success": True})
            else:
                results.append({"accountId": account_id, "success": False, "error": "Database save failed"})

        succeeded = sum(1 for r in results if r["success"])
        failed = len(results) - succeeded
        logger.info(
            "Bulk register for workspace %s: %d succeeded, %d failed out of %d",
            workspace_id, succeeded, failed, len(results),
        )

        return jsonify({"results": results, "succeeded": succeeded, "failed": failed})

    except Exception as e:
        logger.error("Bulk register failed for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts/<account_id>', methods=['DELETE', 'OPTIONS'])
def delete_aws_account(workspace_id, account_id):
    """Disconnect a single AWS account from the workspace."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        from utils.db.connection_utils import delete_connection_secret
        success = delete_connection_secret(user_id, "aws", account_id)
        if success:
            return jsonify({"success": True, "message": f"Account {account_id} disconnected."})
        else:
            return jsonify({"error": "Account not found or already disconnected"}), 404

    except Exception as e:
        logger.error("Failed to delete AWS account %s for workspace %s: %s", account_id, workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts/inactive', methods=['GET', 'OPTIONS'])
def list_inactive_aws_accounts(workspace_id):
    """Return recently disconnected AWS accounts that can be reconnected.

    The IAM role likely still exists in these accounts, so the user can
    reconnect without redeploying the CloudFormation template.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, role_arn, region, last_verified_at "
                    "FROM user_connections "
                    "WHERE user_id = %s AND provider = 'aws' AND status = 'inactive' "
                    "ORDER BY last_verified_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        accounts = [
            {
                "account_id": r[0],
                "role_arn": r[1],
                "region": r[2],
                "disconnected_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]
        return jsonify({"accounts": accounts})

    except Exception as e:
        logger.error("Failed to list inactive accounts for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts/<account_id>/reconnect', methods=['POST', 'OPTIONS'])
def reconnect_aws_account(workspace_id, account_id):
    """Reconnect a previously disconnected AWS account.

    Validates the role still works via STS AssumeRole, then re-activates
    the connection. No CloudFormation redeployment needed.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role_arn, region FROM user_connections "
                    "WHERE user_id = %s AND provider = 'aws' AND account_id = %s AND status = 'inactive'",
                    (user_id, account_id),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            return jsonify({"error": "No inactive connection found for this account"}), 404

        role_arn, region = row[0], row[1] or "us-east-1"

        from utils.aws.aws_sts_client import assume_workspace_role
        try:
            assume_workspace_role(
                role_arn=role_arn,
                external_id=external_id,
                workspace_id=workspace_id,
                duration_seconds=900,
                region=region,
            )
        except Exception as e:
            logger.warning("Role assumption failed for reconnect of account %s: %s", account_id, e)
            return jsonify({
                "error": "Role assumption failed -- the IAM role may have been deleted or the trust policy changed",
            }), 400

        from utils.db.connection_utils import save_connection_metadata
        saved = save_connection_metadata(
            user_id, "aws", account_id,
            role_arn=role_arn,
            connection_method="sts_assume_role",
            region=region,
            status="active",
        )
        if not saved:
            return jsonify({"error": "Failed to persist reconnection"}), 500

        return jsonify({"success": True, "message": f"Account {account_id} reconnected."})

    except Exception as e:
        logger.error("Failed to reconnect account %s for workspace %s: %s", account_id, workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# CloudFormation template endpoint
# ---------------------------------------------------------------------------


@onboarding_bp.route('/workspaces/<workspace_id>/aws/cfn-template', methods=['GET', 'OPTIONS'])
def get_cfn_template(workspace_id):
    """Return the CloudFormation template with ExternalId and Aurora account ID pre-filled.

    Query params:
        format: 'raw' returns plain YAML (default), 'json' returns JSON wrapper
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.aws.aws_sts_client import get_aurora_account_id
        aurora_account_id = get_aurora_account_id()
        if not aurora_account_id:
            return jsonify({"error": "Cannot determine Aurora AWS account ID"}), 500

        template_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "connectors", "aws_connector", "aurora-cross-account-role.yaml",
        )
        template_path = os.path.normpath(template_path)

        with open(template_path, "r") as f:
            template_body = f.read()

        # Replace default parameter values so the downloaded template is ready to deploy
        template_body = template_body.replace(
            "Type: String\n    Description: The 12-digit AWS account ID where Aurora is hosted.",
            f"Type: String\n    Default: '{aurora_account_id}'\n    Description: The 12-digit AWS account ID where Aurora is hosted.",
        )
        template_body = template_body.replace(
            "Type: String\n    Description: >-\n"
            "      Unique external ID generated by Aurora for your tenant.\n"
            "      Find this on the Aurora AWS onboarding page.",
            f"Type: String\n    Default: '{external_id}'\n    Description: >-\n"
            f"      Unique external ID generated by Aurora for your tenant.\n"
            f"      Find this on the Aurora AWS onboarding page.",
        )

        output_format = request.args.get("format", "raw")
        if output_format == "json":
            return jsonify({
                "template": template_body,
                "auroraAccountId": aurora_account_id,
                "externalId": external_id,
            })

        return Response(
            template_body,
            mimetype="application/x-yaml",
            headers={"Content-Disposition": "attachment; filename=aurora-cross-account-role.yaml"},
        )

    except Exception as e:
        logger.error("Failed to generate CFN template for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/cfn-quickcreate', methods=['GET', 'OPTIONS'])
def get_cfn_quickcreate_link(workspace_id):
    """Return a CloudFormation Quick-Create URL that opens the AWS Console
    with all parameters pre-filled.

    The customer logs into the target AWS account, clicks this link, and the
    stack is created with one click -- no CLI or template upload required.

    To deploy org-wide, the customer uses StackSets from their management
    account. Aurora never needs admin access to their accounts.

    Query params:
        region: AWS region for the Console URL (default: us-east-1)
        templateUrl: override the S3 URL for the template (optional,
            for self-hosted deployments that upload the template to S3)
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        user_id = get_authenticated_user_id()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.aws.aws_sts_client import get_aurora_account_id
        aurora_account_id = get_aurora_account_id()
        if not aurora_account_id:
            return jsonify({"error": "Cannot determine Aurora AWS account ID"}), 500

        region = request.args.get("region", "us-east-1")

        # Quick-Create requires the template to be at a public HTTPS URL.
        # Configure AWS_CFN_TEMPLATE_URL in .env pointing to the S3-hosted template.
        template_url = request.args.get("templateUrl") or os.getenv("AWS_CFN_TEMPLATE_URL", "")

        import urllib.parse
        import time as _time
        unique_suffix = hex(int(_time.time()))[2:]  # e.g. "67e1a3b4"
        params = {
            "stackName": f"aurora-role-{unique_suffix}",
            "param_AuroraAccountId": aurora_account_id,
            "param_ExternalId": external_id,
            "param_RoleName": "AuroraReadOnlyRole",
        }
        if template_url:
            params["templateURL"] = template_url

        qs = urllib.parse.urlencode(params)
        console_url = f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}#/stacks/quickcreate?{qs}"

        short_id = external_id[:8]
        stacksets_command = (
            f"aws cloudformation create-stack-set \\\n"
            f"  --stack-set-name aurora-role-{short_id} \\\n"
            f"  --template-body file://aurora-cross-account-role.yaml \\\n"
            f"  --parameters \\\n"
            f"      ParameterKey=AuroraAccountId,ParameterValue={aurora_account_id} \\\n"
            f"      ParameterKey=ExternalId,ParameterValue={external_id} \\\n"
            f"  --capabilities CAPABILITY_NAMED_IAM \\\n"
            f"  --permission-model SERVICE_MANAGED \\\n"
            f"  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false\n\n"
            f"aws cloudformation create-stack-instances \\\n"
            f"  --stack-set-name aurora-role-{short_id} \\\n"
            f"  --deployment-targets OrganizationalUnitIds=<YOUR_ROOT_OU_ID> \\\n"
            f"  --regions {region} \\\n"
            f"  --operation-preferences MaxConcurrentPercentage=100,FailureTolerancePercentage=10"
        )

        return jsonify({
            "quickCreateUrl": console_url,
            "auroraAccountId": aurora_account_id,
            "externalId": external_id,
            "region": region,
            "stackSetsCommand": stacksets_command,
            "note": (
                "Quick-Create link: log into the target AWS account and open this URL. "
                "For org-wide deployment (many accounts), use the StackSets command from "
                "your AWS Organizations management account."
            ),
        })

    except Exception as e:
        logger.error("Failed to generate Quick-Create link for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500
