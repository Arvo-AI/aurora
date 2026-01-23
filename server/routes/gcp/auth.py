from flask import Blueprint, redirect, request, jsonify
import urllib.parse, logging
from connectors.gcp_connector.auth.oauth import (
    get_auth_url,
    exchange_code_for_token,
)
from utils.auth.token_management import store_tokens_in_db
from connectors.gcp_connector.gcp_post_auth_tasks import gcp_post_auth_setup_task
from utils.auth.stateless_auth import get_user_id_from_request
from utils.db.db_utils import connect_to_db_as_admin
from utils.secrets.secret_cache import clear_secret_cache
from time import time
import os

# Blueprint for GCP authentication related routes
# Register this blueprint in main_compute with: app.register_blueprint(gcp_auth_bp)


gcp_auth_bp = Blueprint("gcp_auth_bp", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") + "/chat"

@gcp_auth_bp.route("/", methods=["GET"])
def home():
    """Redirect root to Google OAuth URL."""
    return redirect(get_auth_url())


@gcp_auth_bp.route("/login", methods=["POST"])
def login():
    """Send Google OAuth login URL with user_id encoded in state parameter."""
    logging.info("Logging user in.")

    data = request.get_json()
    user_id = data.get("userId")

    if not user_id:
        return jsonify({"error": "Missing userId"}), 400

    state = urllib.parse.quote(user_id)
    login_url = get_auth_url(state=state)
    return jsonify({"login_url": login_url})


@gcp_auth_bp.route("/callback", methods=["GET", "POST"])
def callback():
    """Handle OAuth callback and exchange the authorization code for tokens."""
    # Retrieve parameters from either form data (POST) or query parameters (GET)
    code = request.form.get("code") if request.method == "POST" else request.args.get("code")
    state = request.form.get("state") if request.method == "POST" else request.args.get("state")
    logging.info("In callback endpoint")

    if not code:
        return jsonify({"error": "Authorization code not provided"}), 400
    if not state:
        return jsonify({"error": "Missing state parameter"}), 400

    try:
        user_id = urllib.parse.unquote(state)
        from time import time  # local import to avoid global dependency loop

        token_data = exchange_code_for_token(code)
        if not token_data:
            logging.error("Failed to exchange OAuth code for tokens")
            return redirect(f"{FRONTEND_URL}?login=failed")

        token_data["expires_at"] = int(time()) + token_data.get("expires_in", 3600)

        # Store tokens against the user
        store_tokens_in_db(user_id, token_data, "gcp")

        # Clear Redis cache to ensure new credentials are used immediately
        try:
            from utils.secrets.secret_cache import clear_secret_cache
            from utils.db.db_utils import connect_to_db_as_admin
            
            # Get the secret_ref from database
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute("SELECT secret_ref FROM user_tokens WHERE user_id = %s AND provider = 'gcp'", (user_id,))
            result = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if result and result[0]:
                clear_secret_cache(result[0])
                logging.info(f"Cleared Redis cache after OAuth reconnect for user {user_id}")
        except Exception as e:
            logging.warning(f"Failed to clear Redis cache: {e}")

        # Kick off async setup tasks
        task = gcp_post_auth_setup_task.delay(user_id)
        logging.info(f"GCP auth callback completed, dispatched async setup task {task.id} for user {user_id}")

        redirect_url = f"{FRONTEND_URL}?login=gcp_setup_pending&task_id={task.id}"
        return redirect(redirect_url)
    except Exception as e:
        logging.error(f"Error during OAuth callback: {e}")
        return redirect(f"{FRONTEND_URL}?login=gcp_failed")


@gcp_auth_bp.route("/gcp/setup/status/<task_id>", methods=["GET"])
def get_gcp_setup_status(task_id):
    """Return status of the async GCP post-auth setup task."""
    try:
        from connectors.gcp_connector.gcp_post_auth_tasks import gcp_post_auth_setup_task
        task = gcp_post_auth_setup_task.AsyncResult(task_id)

        if task.state == "PENDING":
            response = {"state": task.state, "status": "Starting GCP setup", "complete": False, "progress": 0}
        elif task.state == "STARTED":
            response = {"state": task.state, "status": "GCP setup is starting", "complete": False, "progress": 0}
        elif task.state == "PROGRESS":
            # Extract detailed progress information from task meta
            meta = task.info or {}
            current_status = meta.get("status", "Setup in progress")
            progress = meta.get("progress", 0)
            step = meta.get("step", 0)
            total_steps = meta.get("total_steps", 7)
            propagation = meta.get("propagation")
            response = {
                "state": task.state,
                "status": current_status,
                "complete": False,
                "progress": progress,
                "step": step,
                "total_steps": total_steps,
                "propagation": propagation
            }
        elif task.state == "SUCCESS":
            result = task.result or {}
            response = {
                "state": task.state,
                "status": "Setup completed",
                "complete": True,
                "result": result,
            }
        elif task.state == "FAILURE":
            response = {"state": task.state, "status": str(task.info), "complete": True, "error": True}
        else:
            response = {"state": task.state, "status": "Unknown state", "complete": False}
        return jsonify(response)
    except Exception as e:
        logging.error(f"Error fetching task status: {e}")
        return jsonify({"error": "Failed to fetch task status"}), 500


@gcp_auth_bp.route("/api/gcp/force-disconnect", methods=["POST"])
def force_disconnect_gcp():
    """Force disconnect GCP by deleting user tokens and clearing cache."""
    user_id = get_user_id_from_request(request)
    if not user_id:
        return jsonify({"error": "User ID not found"}), 401
    
    logging.info(f"Force disconnecting GCP for user {user_id}")
    
    conn = None
    cursor = None
    secret_ref = None
    
    try:
        # Establish DB connection
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        
        # Get secret_ref before deletion to clear cache
        cursor.execute(
            "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND provider = 'gcp'",
            (user_id,)
        )
        result = cursor.fetchone()
        secret_ref = result[0] if result else None
        
        # Delete GCP tokens from database
        cursor.execute(
            "DELETE FROM user_tokens WHERE user_id = %s AND provider = 'gcp'",
            (user_id,)
        )
        
        # Delete the GCP root project preference from the user_preferences table
        cursor.execute(
            "DELETE FROM user_preferences WHERE user_id = %s AND preference_key = 'gcp_root_project'",
            (user_id,)
        )
        
        # Commit transaction only if all operations succeeded
        conn.commit()
        logging.info(f"Database operations committed for user {user_id}")
        
    except Exception as db_error:
        # Rollback on any database error
        if conn:
            try:
                conn.rollback()
                logging.warning(f"Transaction rolled back for user {user_id}: {db_error}")
            except Exception as rollback_error:
                logging.error(f"Rollback failed: {rollback_error}")
        
        logging.error(f"Database error during GCP force disconnect for user {user_id}: {db_error}")
        return jsonify({"error": "Failed to disconnect GCP"}), 500
        
    finally:
        # Always close cursor and connection
        if cursor:
            try:
                cursor.close()
            except Exception as e:
                logging.warning(f"Failed to close cursor: {e}")
        if conn:
            try:
                conn.close()
            except Exception as e:
                logging.warning(f"Failed to close connection: {e}")
    
    # Clear secret cache if exists (outside transaction)
    if secret_ref:
        try:
            clear_secret_cache(secret_ref)
            logging.info(f"Cleared secret cache for user {user_id}")
        except Exception as e:
            logging.warning(f"Failed to clear secret cache: {e}")
    
    logging.info(f"Successfully force disconnected GCP for user {user_id}")
    return jsonify({"success": True, "message": "GCP disconnected successfully"}), 200


@gcp_auth_bp.route("/gcp/post-auth-retry", methods=["POST"])
def post_auth_retry():
    """Retry post-auth setup with selected projects."""
    try:
        # Authenticate user from session/token - DO NOT trust request body user_id
        authenticated_user_id = get_user_id_from_request()
        if not authenticated_user_id:
            return jsonify({"error": "Unauthorized - authentication required"}), 401
        
        data = request.get_json()
        selected_project_ids = data.get("selected_project_ids", [])
        
        # Use authenticated user ID instead of trusting request body
        user_id = authenticated_user_id
        
        # Validate selected_project_ids is a list
        if not isinstance(selected_project_ids, list):
            return jsonify({"error": "selected_project_ids must be a list"}), 400
        
        # Validate list length
        if not selected_project_ids or len(selected_project_ids) > 5:
            return jsonify({"error": "Must select 1-5 projects"}), 400
        
        # Validate each project ID is a non-empty string
        for project_id in selected_project_ids:
            if not isinstance(project_id, str) or not project_id.strip():
                return jsonify({"error": "All project IDs must be non-empty strings"}), 400
        
        # Trigger the task again with selected projects
        task = gcp_post_auth_setup_task.delay(user_id, selected_project_ids)
        logging.info(f"Retry post-auth setup with {len(selected_project_ids)} projects for user {user_id}, task {task.id}")
        
        return jsonify({"status": "started", "task_id": task.id})
    except Exception as e:
        logging.error(f"Error in post-auth retry: {e}")
        return jsonify({"error": str(e)}), 500

