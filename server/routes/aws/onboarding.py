"""
AWS Onboarding Routes
Manual AWS onboarding via IAM role ARN with STS AssumeRole.
Supports single-account and multi-account (bulk) onboarding.
"""
import logging
import os
from flask import Blueprint, request, jsonify, Response
from utils.web.cors_utils import create_cors_response
from utils.auth.rbac_decorators import require_permission
from utils.workspace.workspace_utils import (
    get_or_create_workspace,
    get_workspace_by_id,
    update_workspace_aws_role,
    is_workspace_aws_configured,
    get_workspace_aws_status
)

logger = logging.getLogger(__name__)

CLOUDFORMATION_TEMPLATE_URL = "https://aurora-cfn-templates-390403884122.s3.ca-central-1.amazonaws.com/aurora-cross-account-role.yaml"

onboarding_bp = Blueprint("aws_onboarding_bp", __name__)


@onboarding_bp.route('/aws/env/check', methods=['GET', 'OPTIONS'])
@require_permission("connectors", "read")
def check_aws_environment(_user_id):
    """
    Check if Aurora has AWS credentials available via any method
    (env vars, IRSA web identity, instance profile, etc.).
    
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
        
        from utils.aws.aws_sts_client import get_aurora_account_id
        account_id = get_aurora_account_id()

        if not configured and account_id:
            configured = True
            logger.info("AWS credentials available via boto3 credential chain (IRSA/instance profile)")
        
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
@require_permission("connectors", "read")
def get_aws_onboarding_links(user_id, workspace_id):
    """
    Get AWS onboarding information for a workspace (external ID and status).
    
    Returns basic information needed for manual role setup.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404
        
        if workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        from utils.aws.aws_sts_client import get_aurora_account_id
        aurora_account_id = get_aurora_account_id()
        
        response_data = {
            "workspaceId": workspace_id,
            "externalId": workspace['aws_external_id'],
            "status": get_workspace_aws_status(workspace)
        }

        if aurora_account_id:
            response_data["auroraAccountId"] = aurora_account_id

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
@require_permission("connectors", "write")
def set_aws_role(user_id, workspace_id):
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
        
        if not role_arn.startswith('arn:aws:iam::'):
            return jsonify({"error": "Invalid role ARN format"}), 400

        read_only_role_arn = data.get('readOnlyRoleArn') or data.get('read_only_role_arn')

        from utils.aws.aws_sts_client import assume_workspace_role, get_aurora_account_id

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
@require_permission("connectors", "read")
def get_aws_onboarding_status(user_id, workspace_id):
    """
    Get current AWS onboarding status for a workspace.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            return jsonify({"error": "Workspace not found"}), 404
        
        if workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        status = get_workspace_aws_status(workspace)
        
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



@onboarding_bp.route('/users/<user_id>/workspaces', methods=['GET', 'OPTIONS'])
@require_permission("connectors", "read")
def list_user_workspaces(authenticated_user_id, user_id):
    """Get user workspaces."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        if authenticated_user_id != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        workspace = get_or_create_workspace(user_id, "default")
        return jsonify({"workspaces": [workspace]})
        
    except Exception as e:
        logger.error(f"Failed to list workspaces for user {user_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/users/<user_id>/workspaces', methods=['POST'])
@require_permission("connectors", "write")
def create_user_workspace(authenticated_user_id, user_id):
    """Create new workspace."""
    try:
        if authenticated_user_id != user_id:
            return jsonify({"error": "Access denied"}), 403
        
        data = request.get_json() or {}
        workspace_name = data.get('name', 'default')
        
        workspace = get_or_create_workspace(user_id, workspace_name)
        return jsonify(workspace)
        
    except Exception as e:
        logger.error(f"Failed to create workspace for user {user_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500



@onboarding_bp.route('/workspaces/<workspace_id>/aws/cleanup', methods=['POST', 'OPTIONS'])
@require_permission("connectors", "write")
def workspace_cleanup(user_id, workspace_id):
    """Disconnect AWS connection by removing it from user_connections (single source of truth).
    
    Users must manually remove IAM roles and other AWS resources in their AWS console.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

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

        account_id = aws_conn.get('account_id')
        if account_id:
            success = delete_connection_secret(user_id, "aws", account_id)
            if not success:
                logger.error("Failed to delete AWS connection for user %s account %s", user_id, account_id)
                return jsonify({"error": "Failed to disconnect AWS connection"}), 500
        
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
@require_permission("connectors", "read")
def list_aws_accounts(user_id, workspace_id):
    """Return all active AWS accounts connected to this workspace's owner."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
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
@require_permission("connectors", "write")
def bulk_register_aws_accounts(user_id, workspace_id):
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
        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        data = request.get_json()
        if not data or not isinstance(data.get("accounts"), list):
            return jsonify({"error": "Payload must contain an 'accounts' array"}), 400

        MAX_BULK_ACCOUNTS = 50
        if len(data["accounts"]) > MAX_BULK_ACCOUNTS:
            return jsonify({
                "error": f"Too many accounts. Maximum {MAX_BULK_ACCOUNTS} per bulk request to avoid STS rate limits."
            }), 400

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.aws.aws_sts_client import assume_workspace_role
        from utils.db.connection_utils import save_connection_metadata, extract_account_id_from_arn

        results = []
        for entry in data["accounts"]:
            if not isinstance(entry, dict):
                results.append({"accountId": "unknown", "success": False, "error": "Each entry must be a JSON object"})
                continue
            role_arn = (entry.get("roleArn") or "").strip()
            account_id = (entry.get("accountId") or "").strip()
            region = (entry.get("region") or "us-east-1").strip()

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
                workspace_id=workspace_id,
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
@require_permission("connectors", "write")
def delete_aws_account(user_id, workspace_id, account_id):
    """Disconnect a single AWS account from the workspace."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
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
@require_permission("connectors", "read")
def list_inactive_aws_accounts(user_id, workspace_id):
    """Return recently disconnected AWS accounts that can be reconnected.

    The IAM role likely still exists in these accounts, so the user can
    reconnect without redeploying the CloudFormation template.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        from utils.db.connection_utils import get_inactive_aws_connections
        accounts = get_inactive_aws_connections(user_id)
        return jsonify({"accounts": accounts})

    except Exception as e:
        logger.error("Failed to list inactive accounts for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/accounts/<account_id>/reconnect', methods=['POST', 'OPTIONS'])
@require_permission("connectors", "write")
def reconnect_aws_account(user_id, workspace_id, account_id):
    """Reconnect a previously disconnected AWS account.

    Validates the role still works via STS AssumeRole, then re-activates
    the connection. No CloudFormation redeployment needed.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        workspace = get_workspace_by_id(workspace_id)
        if not workspace or workspace['user_id'] != user_id:
            return jsonify({"error": "Access denied"}), 403

        external_id = workspace.get("aws_external_id")
        if not external_id:
            return jsonify({"error": "Workspace missing aws_external_id"}), 500

        from utils.db.connection_utils import get_inactive_aws_connection
        inactive_conn = get_inactive_aws_connection(user_id, account_id)

        if not inactive_conn:
            return jsonify({"error": "No inactive connection found for this account"}), 404

        role_arn = inactive_conn["role_arn"]
        region = inactive_conn["region"] or "us-east-1"

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
            workspace_id=workspace_id,
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
@require_permission("connectors", "read")
def get_cfn_template(user_id, workspace_id):
    """Return the CloudFormation template with ExternalId and Aurora account ID pre-filled.

    Query params:
        format: 'raw' returns plain YAML (default), 'json' returns JSON wrapper
    """
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
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

        import yaml

        class _CfnTag:
            """Wrapper to preserve CloudFormation intrinsic function tags during round-trip."""
            def __init__(self, tag, value):
                self.tag = tag
                self.value = value

        def _cfn_constructor(loader, tag_suffix, node):
            if isinstance(node, yaml.ScalarNode):
                return _CfnTag(tag_suffix, loader.construct_scalar(node))
            elif isinstance(node, yaml.SequenceNode):
                return _CfnTag(tag_suffix, loader.construct_sequence(node))
            elif isinstance(node, yaml.MappingNode):
                return _CfnTag(tag_suffix, loader.construct_mapping(node))
            return _CfnTag(tag_suffix, None)

        def _cfn_representer(dumper, data):
            tag = "!" + data.tag
            if isinstance(data.value, list):
                return dumper.represent_sequence(tag, data.value)
            elif isinstance(data.value, dict):
                return dumper.represent_mapping(tag, data.value)
            return dumper.represent_scalar(tag, data.value)

        CfnLoader = type("CfnLoader", (yaml.SafeLoader,), {})
        CfnLoader.add_multi_constructor("!", _cfn_constructor)

        CfnDumper = type("CfnDumper", (yaml.Dumper,), {})
        CfnDumper.add_representer(_CfnTag, _cfn_representer)

        template_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "connectors", "aws_connector", "aurora-cross-account-role.yaml",
        )
        template_path = os.path.normpath(template_path)

        with open(template_path, "r") as f:
            template = yaml.load(f, Loader=CfnLoader)

        params = template.get("Parameters", {})
        if "AuroraAccountId" in params:
            params["AuroraAccountId"]["Default"] = aurora_account_id
        if "ExternalId" in params:
            params["ExternalId"]["Default"] = external_id

        role_type = request.args.get("roleType", "ReadOnly")
        if role_type == "Admin" and "RoleType" in params:
            params["RoleType"]["Default"] = "Admin"

        template_body = yaml.dump(template, Dumper=CfnDumper, default_flow_style=False, sort_keys=False)

        filename = f"aurora-{'admin' if role_type == 'Admin' else 'readonly'}-role.yaml"

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
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except Exception as e:
        logger.error("Failed to generate CFN template for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500


@onboarding_bp.route('/workspaces/<workspace_id>/aws/cfn-quickcreate', methods=['GET', 'OPTIONS'])
@require_permission("connectors", "read")
def get_cfn_quickcreate_link(user_id, workspace_id):
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
        # Aurora hosts this template publicly on its own AWS account.
        template_url = request.args.get("templateUrl") or CLOUDFORMATION_TEMPLATE_URL

        import urllib.parse
        import secrets as _secrets
        unique_suffix = _secrets.token_hex(4)
        role_type = request.args.get("roleType", "ReadOnly")
        if role_type not in ("ReadOnly", "Admin"):
            role_type = "ReadOnly"
        params = {
            "stackName": f"aurora-role-{unique_suffix}",
            "templateURL": template_url,
            "param_AuroraAccountId": aurora_account_id,
            "param_ExternalId": external_id,
            "param_RoleType": role_type,
        }

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
            f"      ParameterKey=RoleType,ParameterValue={role_type} \\\n"
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
            "templateUrl": template_url,
            "stackSetsCommand": stacksets_command,
            "note": (
                "Quick-Create link: log into the target AWS account and open this URL. "
                "Aurora hosts the CloudFormation template publicly — no setup required. "
                "For org-wide deployment (many accounts), use the StackSets command from "
                "your AWS Organizations management account."
            ),
        })

    except Exception as e:
        logger.error("Failed to generate Quick-Create link for workspace %s: %s", workspace_id, e)
        return jsonify({"error": "Internal server error"}), 500
