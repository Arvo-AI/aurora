"""
GitHub repository selection storage endpoints
"""
import logging
import json
from flask import Blueprint, jsonify, request
from utils.auth.stateless_auth import get_credentials_from_db, get_user_id_from_request
from utils.auth.token_management import store_tokens_in_db
from utils.db.db_utils import connect_to_db_as_user

github_repo_selection_bp = Blueprint('github_repo_selection', __name__)
logger = logging.getLogger(__name__)

def create_cors_response(data=None, status=200):
    """Create a response with CORS headers"""
    response = jsonify(data) if data else jsonify({})
    response.status_code = status
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-User-ID, Authorization'
    return response

def get_user_id_from_request():
    """Extract user ID from request headers"""
    return request.headers.get('X-User-ID') or request.headers.get('X-User-ID')

@github_repo_selection_bp.route("/repo-selection", methods=["GET", "OPTIONS"])
def get_repo_selection():
    """Get the stored GitHub repository selection for a user"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return create_cors_response({"error": "User ID required"}, 400)
        
        # Get stored GitHub repo selection
        repo_selection = get_credentials_from_db(user_id, "github_repo_selection")
        
        if repo_selection:
            return create_cors_response({
                "repository": repo_selection.get("repository"),
                "branch": repo_selection.get("branch")
            })
        else:
            return create_cors_response({
                "repository": None,
                "branch": None
            })
        
    except Exception as e:
        logger.error(f"Error getting repo selection: {e}", exc_info=True)
        return create_cors_response({"error": "Failed to get repository selection"}, 500)

@github_repo_selection_bp.route("/repo-selection", methods=["POST", "PUT"])
def save_repo_selection():
    """Save the GitHub repository selection for a user"""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return create_cors_response({"error": "User ID required"}, 400)
        
        data = request.get_json()
        if not data:
            return create_cors_response({"error": "Request body required"}, 400)
        
        repository = data.get("repository")
        branch = data.get("branch")
        
        if not repository or not branch:
            return create_cors_response({"error": "Repository and branch are required"}, 400)
        
        # Validate repository structure
        required_repo_fields = ["id", "name", "full_name", "private", "default_branch", "owner"]
        for field in required_repo_fields:
            if field not in repository:
                return create_cors_response({"error": f"Repository missing required field: {field}"}, 400)
        
        # Validate branch structure
        required_branch_fields = ["name"]
        for field in required_branch_fields:
            if field not in branch:
                return create_cors_response({"error": f"Branch missing required field: {field}"}, 400)
        
        # Store the selection
        selection_data = {
            "repository": repository,
            "branch": branch,
            "updated_at": None  # This will be set by store_credentials_in_db
        }
        
        store_tokens_in_db(user_id, selection_data, "github_repo_selection")
        
        logger.info(f"Saved GitHub repo selection for user {user_id}: {repository['full_name']} / {branch['name']}")
        
        return create_cors_response({
            "message": "Repository selection saved successfully",
            "repository": repository,
            "branch": branch
        })
        
    except Exception as e:
        logger.error(f"Error saving repo selection: {e}", exc_info=True)
        return create_cors_response({"error": "Failed to save repository selection"}, 500)

@github_repo_selection_bp.route("/repo-selection", methods=["DELETE"])
def clear_repo_selection():
    """Clear the GitHub repository selection for a user"""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return create_cors_response({"error": "User ID required"}, 400)
        
        # Clear the stored selection by deleting the record
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        cursor.execute(
            "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
            (user_id, "github_repo_selection")
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        logger.info(f"Cleared GitHub repo selection for user {user_id}")
        
        return create_cors_response({
            "message": "Repository selection cleared successfully"
        })
        
    except Exception as e:
        logger.error(f"Error clearing repo selection: {e}", exc_info=True)
        return create_cors_response({"error": "Failed to clear repository selection"}, 500)
