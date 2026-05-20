"""
API endpoints for managing GCP root project selection.
"""

import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import (
    store_user_preference,
    get_user_preference,
    get_credentials_from_db,
)
from utils.auth.rbac_decorators import require_permission
from connectors.gcp_connector.auth.oauth import get_credentials
from routes.gcp.root_project_tasks import setup_root_project_async
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

root_project_bp = Blueprint('root_project', __name__)

@root_project_bp.route('/api/gcp/root-project', methods=['GET'])
@require_permission("connectors", "read")
def get_root_project(user_id):
    """Get the currently selected root project for the user."""
    try:
        root_project = get_user_preference(user_id, 'gcp_root_project')

        sa_info = get_user_preference(user_id, 'gcp_service_accounts')

        return jsonify({
            "root_project": root_project,
            "service_accounts": sa_info
        })
    except Exception as e:
        logger.error(f"Error getting root project: {e}", exc_info=True)
        return jsonify({"error": "Failed to get root project"}), 500

@root_project_bp.route('/api/gcp/root-project', methods=['POST'])
@require_permission("connectors", "write")
def set_root_project(user_id):
    """Set the root project for service account creation."""
    try:

        data = request.get_json()
        project_id = data.get('project_id')

        if not project_id:
            return jsonify({"error": "Missing project_id"}), 400

        # Get user credentials
        token_data = get_credentials_from_db(user_id, 'gcp')
        if not token_data:
            return jsonify({"error": "No GCP credentials found"}), 401

        credentials = get_credentials(token_data)

        # Validate the project
        validation_result = validate_root_project(credentials, project_id)
        if not validation_result['valid']:
            return jsonify({
                "error": f"Project cannot be used as root: {validation_result['reason']}"
            }), 400

        # Store the preference
        store_user_preference(user_id, 'gcp_root_project', project_id)

        # Trigger service account setup with new root project
        logger.info(f"Setting root project to {project_id} for user {user_id}")

        try:
            task = setup_root_project_async.delay(user_id, project_id)
            logger.info(
                "Queued root project setup task %s for user %s project %s",
                task.id,
                user_id,
                project_id,
            )
        except Exception as enqueue_error:
            logger.error(f"Failed to enqueue root project setup task: {enqueue_error}")
            return jsonify({
                "success": False,
                "preference_saved": True,
                "service_account_setup": "failed_to_enqueue",
                "root_project": project_id,
                "error": "Preference saved but provisioning task could not be queued"
            }), 500

        return jsonify({
            "success": True,
            "preference_saved": True,
            "service_account_setup": "pending",
            "root_project": project_id,
            "task_id": task.id,
            "message": "Root project preference saved. Service account provisioning is running asynchronously."
        }), 202

    except Exception as e:
        logger.error(f"Error setting root project: {e}", exc_info=True)
        return jsonify({"error": "Failed to set root project"}), 500

def validate_root_project(credentials, project_id):
    """Validate if a project can be used as root project.

    Only requirement: user must have IAM permission to create a service
    account in this project. Billing is NOT required for SA creation.
    """
    try:
        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
        try:
            crm_service.projects().getIamPolicy(
                resource=project_id,
                body={}
            ).execute()
        except HttpError as e:
            if e.resp.status == 403:
                return {"valid": False, "reason": "Insufficient IAM permissions"}
            raise

        return {"valid": True, "reason": "Project meets all requirements"}

    except Exception as e:
        logger.error(f"Error validating project {project_id}: {e}", exc_info=True)
        return {"valid": False, "reason": "Failed to validate project"}
