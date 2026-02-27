"""
Bitbucket workspace selection storage endpoints.
Stores the user's selected workspace, repository, and branch.
"""
import logging

from flask import Blueprint, jsonify, request

from utils.auth.stateless_auth import get_credentials_from_db, get_user_id_from_request
from utils.auth.token_management import store_tokens_in_db
from utils.web.cors_utils import create_cors_response

bitbucket_selection_bp = Blueprint("bitbucket_selection", __name__)
logger = logging.getLogger(__name__)


@bitbucket_selection_bp.route("/workspace-selection", methods=["GET", "OPTIONS"])
def get_workspace_selection():
    """Get the stored Bitbucket workspace selection for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        selection = get_credentials_from_db(user_id, "bitbucket_workspace_selection") or {}

        return jsonify({
            "workspace": selection.get("workspace"),
            "repository": selection.get("repository"),
            "branch": selection.get("branch"),
        })

    except Exception as e:
        logger.error(f"Error getting workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to get workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["POST", "PUT", "OPTIONS"])
def save_workspace_selection():
    """Save the Bitbucket workspace selection for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400

        workspace = data.get("workspace")
        repository = data.get("repository")
        branch = data.get("branch")

        if not workspace or not repository or not branch:
            return jsonify({"error": "Workspace, repository, and branch are required"}), 400

        selection_data = {
            "workspace": workspace,
            "repository": repository,
            "branch": branch,
        }

        store_tokens_in_db(user_id, selection_data, "bitbucket_workspace_selection")

        ws_name = workspace.get('slug', workspace) if isinstance(workspace, dict) else workspace
        repo_name = repository.get('name', repository) if isinstance(repository, dict) else repository
        branch_name = branch.get('name', branch) if isinstance(branch, dict) else branch
        logger.info(f"Saved Bitbucket workspace selection for user {user_id}: {ws_name} / {repo_name} / {branch_name}")

        return jsonify({
            "message": "Workspace selection saved successfully",
            "workspace": workspace,
            "repository": repository,
            "branch": branch,
        })

    except Exception as e:
        logger.error(f"Error saving workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to save workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["DELETE", "OPTIONS"])
def clear_workspace_selection():
    """Clear the Bitbucket workspace selection for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        from utils.secrets.secret_ref_utils import delete_user_secret

        delete_user_secret(user_id, "bitbucket_workspace_selection")

        logger.info(f"Cleared Bitbucket workspace selection for user {user_id}")
        return jsonify({"message": "Workspace selection cleared successfully"})

    except Exception as e:
        logger.error(f"Error clearing workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to clear workspace selection"}), 500
