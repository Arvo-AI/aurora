"""User preferences API routes for stateless session management."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import (
    get_user_id_from_request, 
    store_user_preference, 
    get_user_preference,
    get_credentials_from_db,
    create_cors_response
)
import json

# Configure logging
logger = logging.getLogger(__name__)

user_preferences_bp = Blueprint('user_preferences', __name__)

@user_preferences_bp.route('/api/user-preferences', methods=['GET', 'POST', 'OPTIONS'])
def handle_user_preferences():
    """Handle user preferences storage and retrieval."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        logger.warning("Missing user_id in user preferences request")
        return jsonify({"error": "Missing user_id"}), 400
    
    if request.method == 'POST':
        data = request.get_json()
        key = data.get('key')
        value = data.get('value')
        
        if not key:
            logger.warning(f"Missing preference key for user {user_id}")
            return jsonify({"error": "Missing preference key"}), 400
        
        store_user_preference(user_id, key, value)
        logger.info(f"Stored preference {key} for user {user_id}")
        return jsonify({"status": "success"})
    
    else:  # GET
        key = request.args.get('key')
        if not key:
            logger.warning(f"Missing preference key for user {user_id}")
            return jsonify({"error": "Missing preference key"}), 400
        
        value = get_user_preference(user_id, key)
        logger.debug(f"Retrieved preference {key} for user {user_id}")
        return jsonify({"value": value})

@user_preferences_bp.route('/api/clear-session', methods=['POST', 'OPTIONS'])
def clear_session():
    """Clear all user session data from database."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        logger.warning("Missing user_id in clear session request")
        return jsonify({"error": "Missing user_id"}), 400
    
    try:
        from utils.db.db_utils import connect_to_db_as_user
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Clear all user preferences (session-like data)
        cursor.execute("DELETE FROM user_preferences WHERE user_id = %s", (user_id,))
        
        # Optionally clear deployment tasks
        cursor.execute("DELETE FROM deployment_tasks WHERE user_id = %s", (user_id,))
        
        conn.commit()
        logger.info(f"Cleared session data for user {user_id}")
        return jsonify({"status": "success"})
        
    except Exception as e:
        logger.error(f"Error clearing session for user {user_id}: {e}")
        return jsonify({"error": "Failed to clear session"}), 500
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

@user_preferences_bp.route('/api/credentials/<provider>', methods=['GET', 'OPTIONS'])
def get_credentials(provider):
    """Get provider credentials from database."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        logger.warning(f"Missing user_id in get credentials request for {provider}")
        return jsonify({"error": "Missing user_id"}), 400
    
    credentials = get_credentials_from_db(user_id, provider)
    if credentials:
        logger.info(f"Retrieved {provider} credentials for user {user_id}")
        return jsonify(credentials)
    else:
        logger.warning(f"No {provider} credentials found for user {user_id}")
        return jsonify({"error": f"No {provider} credentials found"}), 404

@user_preferences_bp.route('/api/user-preferences/batch', methods=['GET', 'POST', 'OPTIONS'])
def handle_batch_preferences():
    """Handle batch operations for user preferences."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        logger.warning("Missing user_id in batch preferences request")
        return jsonify({"error": "Missing user_id"}), 400
    
    if request.method == 'POST':
        # Store multiple preferences at once
        data = request.get_json()
        preferences = data.get('preferences', {})
        
        if not isinstance(preferences, dict):
            return jsonify({"error": "preferences must be a dictionary"}), 400
        
        try:
            for key, value in preferences.items():
                store_user_preference(user_id, key, value)
            
            logger.info(f"Stored {len(preferences)} preferences for user {user_id}")
            return jsonify({"status": "success", "count": len(preferences)})
        except Exception as e:
            logger.error(f"Error storing batch preferences for user {user_id}: {e}")
            return jsonify({"error": "Failed to store preferences"}), 500
    
    else:  # GET
        # Retrieve multiple preferences at once
        keys = request.args.getlist('keys')
        
        if not keys:
            return jsonify({"error": "No keys specified"}), 400
        
        try:
            from utils.db.db_utils import connect_to_db_as_user
            conn = connect_to_db_as_user()
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            
            # Build query for multiple keys
            placeholders = ','.join(['%s'] * len(keys))
            cursor.execute(
                f"SELECT preference_key, preference_value FROM user_preferences WHERE user_id = %s AND preference_key IN ({placeholders})",
                [user_id] + keys
            )
            results = cursor.fetchall()
            
            # Build response dictionary
            preferences = {}
            for key, value in results:
                # Handle JSONB values that are already decoded by PostgreSQL
                if value is not None:
                    if isinstance(value, str):
                        try:
                            preferences[key] = json.loads(value)
                        except json.JSONDecodeError:
                            preferences[key] = value
                    else:
                        # Value is already a Python object (from JSONB)
                        preferences[key] = value
                else:
                    preferences[key] = None
            
            # Add missing keys with None values
            for key in keys:
                if key not in preferences:
                    preferences[key] = None
            
            logger.debug(f"Retrieved {len(preferences)} preferences for user {user_id}")
            return jsonify({"preferences": preferences})
            
        except Exception as e:
            logger.error(f"Error retrieving batch preferences for user {user_id}: {e}")
            return jsonify({"error": "Failed to retrieve preferences"}), 500
        finally:
            if 'cursor' in locals() and cursor:
                cursor.close()
            if 'conn' in locals() and conn:
                conn.close()

@user_preferences_bp.route('/api/terraform/clear-state', methods=['POST', 'OPTIONS'])
def clear_terraform_state():
    """
    Clear Terraform state files for the current user.
    This removes terraform.tfstate, .terraform.lock.hcl, and .terraform directory.
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            logger.warning("Missing user_id in clear terraform state request")
            return jsonify({"error": "Missing user_id"}), 400
        
        logger.info(f"Clearing Terraform state for user {user_id}")
        
        # Import the required functions
        from chat.backend.agent.tools.iac.iac_write_tool import get_terraform_directory
        
        # Get user's terraform directory (without session_id to get the user-level directory)
        user_terraform_dir = get_terraform_directory(user_id)
        
        # Check for state files in all session directories and the user directory itself
        files_existed = []
        all_cleared_files = []
        
        # Check user-level directory first (for backward compatibility)
        for file_name, file_path in [
            ("terraform.tfstate", user_terraform_dir / "terraform.tfstate"),
            (".terraform.lock.hcl", user_terraform_dir / ".terraform.lock.hcl"),
            (".terraform directory", user_terraform_dir / ".terraform")
        ]:
            if file_path.exists():
                files_existed.append(file_name)
        
        # Check all session directories
        if user_terraform_dir.exists():
            for session_dir in user_terraform_dir.glob("session_*"):
                if session_dir.is_dir():
                    for file_name, file_path in [
                        ("terraform.tfstate", session_dir / "terraform.tfstate"),
                        (".terraform.lock.hcl", session_dir / ".terraform.lock.hcl"),
                        (".terraform directory", session_dir / ".terraform")
                    ]:
                        if file_path.exists():
                            session_relative_name = f"{session_dir.name}/{file_name}"
                            files_existed.append(session_relative_name)
        
        if not files_existed:
            return jsonify({
                "success": True,
                "message": "No Terraform state files found to clear",
                "files_cleared": []
            }), 200
        
        # Force clear all Terraform state files
        try:
            import shutil
            
            # Clear user-level files first (for backward compatibility)
            for file_name, file_path in [
                ("terraform.tfstate", user_terraform_dir / "terraform.tfstate"),
                (".terraform.lock.hcl", user_terraform_dir / ".terraform.lock.hcl"),
                (".terraform directory", user_terraform_dir / ".terraform")
            ]:
                if file_path.exists():
                    if file_path.is_dir():
                        shutil.rmtree(file_path)
                    else:
                        file_path.unlink()
                    all_cleared_files.append(file_name)
                    logger.info(f"Manually cleared user-level {file_name}")
            
            # Clear all session directories
            if user_terraform_dir.exists():
                for session_dir in user_terraform_dir.glob("session_*"):
                    if session_dir.is_dir():
                        for file_name, file_path in [
                            ("terraform.tfstate", session_dir / "terraform.tfstate"),
                            (".terraform.lock.hcl", session_dir / ".terraform.lock.hcl"),
                            (".terraform directory", session_dir / ".terraform")
                        ]:
                            if file_path.exists():
                                if file_path.is_dir():
                                    shutil.rmtree(file_path)
                                else:
                                    file_path.unlink()
                                session_relative_name = f"{session_dir.name}/{file_name}"
                                all_cleared_files.append(session_relative_name)
                                logger.info(f"Manually cleared {session_relative_name}")
            
            logger.info(f"Successfully cleared Terraform state for user {user_id}: {', '.join(all_cleared_files)}")
            
            return jsonify({
                "success": True,
                "message": f"Successfully cleared Terraform state files: {', '.join(all_cleared_files)}",
                "files_cleared": all_cleared_files
            }), 200
            
        except Exception as clear_error:
            logger.error(f"Error clearing Terraform state files for user {user_id}: {clear_error}")
            return jsonify({
                "success": False,
                "error": f"Failed to clear some Terraform state files: {str(clear_error)}"
            }), 500
        
    except Exception as e:
        logger.error(f"Error in clear_terraform_state endpoint: {str(e)}")
        import traceback
        error_traceback = traceback.format_exc()
        logger.error(f"Traceback:\n{error_traceback}")
        return jsonify({
            "success": False,
            "error": f"An unexpected error occurred: {str(e)}"
        }), 500 