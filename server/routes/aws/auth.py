"""
AWS Authentication Routes
"""
import logging
from flask import Blueprint, request, jsonify, session
import flask
import boto3
from botocore.exceptions import ClientError
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.logging.secure_logging import mask_credential_value
from utils.workspace.workspace_utils import (
    get_or_create_workspace,
    is_workspace_aws_configured,
    update_workspace_aws_role,
)

auth_bp = Blueprint("aws_auth_bp", __name__)

@auth_bp.route('/get-credentials', methods=['POST', 'OPTIONS'])
def aws_get_credentials():
    """Retrieve AWS credentials stored for the user."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    try:
        data = request.get_json()
        user_id = data.get("userId")
        if not user_id:
            return jsonify({"error": "Missing userId"}), 400

        # Authorize caller
        authenticated_user_id = get_user_id_from_request()
        if not authenticated_user_id or authenticated_user_id != user_id:
            logging.warning("Unauthorized access to AWS creds")
            return jsonify({"error": "Unauthorized"}), 401

        # Try session cache first
        aws_credentials = session.get('aws_credentials')
        if not aws_credentials:
            try:
                # Single source of truth: read from user_connections
                from utils.db.connection_utils import get_user_aws_connection
                aws_conn = get_user_aws_connection(user_id)
                
                if aws_conn:
                    # Get external_id from workspace (needed for STS)
                    workspace = get_or_create_workspace(user_id, "default")
                    session['aws_credentials'] = {
                        'role_arn': aws_conn.get('role_arn'),
                        'external_id': workspace.get('aws_external_id'),
                        'aws_account_id': aws_conn.get('account_id', 'Unknown')
                    }
                    aws_credentials = session['aws_credentials']
                    logging.info(f"Retrieved AWS role credentials from user_connections for user {user_id}")
            except Exception as e:
                logging.error(f"Error retrieving AWS creds: {e}")

        if aws_credentials:
            session['user_id'] = user_id
            safe_response = {
                "status": "success",
                "message": "AWS role credentials found",
                "has_credentials": True,
                "role_arn": aws_credentials.get('role_arn'),
                "account_id": aws_credentials.get('aws_account_id', 'Unknown')
            }
            return jsonify(safe_response)
        else:
            return jsonify({
                "error": "No AWS credentials found. Please authenticate with AWS."
            }), 401
    except Exception as e:
        logging.error(f"Error retrieving AWS credentials: {e}")
        return jsonify({"error": str(e)}), 500


@auth_bp.route('/auth', methods=['POST', 'OPTIONS'])
def auth():
    """
    AWS authentication endpoint using IAM role assumption.
    
    Requires External ID that matches the workspace's External ID for security.
    Legacy flow without External ID is no longer supported.
    """
    if flask.request.method == 'OPTIONS':
        return create_cors_response()

    logging.info("=== AWS AUTH ENDPOINT STARTED ===")
    try:
        data = flask.request.get_json()
        role_arn = data.get('role_arn')
        read_only_role_arn = data.get('read_only_role_arn') or data.get('readOnlyRoleArn')
        external_id = data.get('external_id')
        user_id = data.get('userId')

        # Validate inputs
        if not user_id:
            return jsonify({"status": "error", "message": "Missing userId"}), 400
        if not role_arn:
            return jsonify({"status": "error", "message": "Missing role_arn"}), 400

        from utils.workspace.workspace_utils import get_or_create_workspace, update_workspace_aws_role
        workspace = get_or_create_workspace(user_id, "default")
        workspace_external_id = workspace.get('aws_external_id')

        # SECURITY: External ID is now REQUIRED - no legacy flow allowed
        # This prevents attackers from assuming roles without proper External ID validation
        if not external_id:
            logging.error(f"User {user_id} attempted role assumption without external_id. Rejected for security.")
            return jsonify({
                "status": "error",
                "message": "External ID is required for AWS role assumption. Please provide the External ID from your workspace."
            }), 400

        # SECURITY: Validate external_id matches workspace_external_id BEFORE attempting role assumption
        # This prevents attackers from using incorrect or missing External IDs
        if external_id != workspace_external_id:
            logging.error(
                f"User {user_id} provided external_id '{external_id}' that does not match workspace external_id '{workspace_external_id}'. Rejected for security."
            )
            return jsonify({
                "status": "error",
                "message": "Invalid external_id. Please use the External ID provided by your workspace."
            }), 401

        # Assume the role - ExternalId is now always required
        try:
            sts = boto3.client("sts")
            assume_role_kwargs = {
                "RoleArn": role_arn,
                "RoleSessionName": f"aurora-{user_id}",
                "ExternalId": external_id  # Always include ExternalId - no longer optional
            }

            response = sts.assume_role(**assume_role_kwargs)
            credentials = response["Credentials"]
            
            # Get account ID from the assumed role
            temp_session = boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )
            account_id = temp_session.client("sts").get_caller_identity()["Account"]

            # This saves to user_connections (single source of truth)
            update_workspace_aws_role(workspace['id'], role_arn, read_only_role_arn=read_only_role_arn)
            logging.info(f"Updated AWS connection in user_connections for user {user_id} via workspace {workspace['id']}")

            return jsonify({
                "status": "success",
                "message": "Assume-Role successful",
                "account_id": account_id,
                "expires_at": credentials["Expiration"].isoformat(),
            })
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "AccessDenied":
                logging.error(f"AssumeRole AccessDenied for user {user_id} and role {role_arn}. Check ExternalId and IAM trust policy.")
                
                # Extract account ID from role ARN for better error messaging
                try:
                    account_id = role_arn.split(':')[4]
                except (IndexError, AttributeError):
                    account_id = "your AWS account"
                
                # Auto-detect Aurora's account ID
                from utils.aws.aws_sts_client import get_aurora_account_id
                aurora_account_id = get_aurora_account_id()
                if not aurora_account_id:
                    logging.error("Could not determine Aurora's AWS account ID. Ensure Aurora has AWS credentials configured.")
                    return jsonify({
                        "status": "error",
                        "message": "Server configuration error: Unable to determine Aurora's AWS account ID. Please ensure Aurora has AWS credentials configured."
                    }), 500
                
                error_message = (
                    f"Access denied when assuming role. Please verify:\n\n"
                    f"1. The role exists in account {account_id}\n"
                    f"2. The role's trust policy includes Aurora as a trusted entity:\n"
                    f"   - Principal: arn:aws:iam::{aurora_account_id}:root\n"
                    f"   - ExternalId: {external_id or 'Not provided'}\n\n"
                    f"3. Aurora has the necessary permissions to assume this role\n\n"
                    f"Double-check the IAM console and ensure the trust relationship is configured correctly."
                )
                
                return jsonify({
                    "status": "error",
                    "message": error_message,
                    "details": {
                        "role_arn": role_arn,
                        "external_id": external_id,
                        "account_id": account_id,
                        "error_code": error_code
                    }
                }), 401
            else:
                logging.error(f"AssumeRole failed for user {user_id}: {e}")
                return jsonify({"status": "error", "message": str(e)}), 401

    except Exception as e:
        logging.error("Unexpected error in AWS auth", exc_info=e)
        return jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
