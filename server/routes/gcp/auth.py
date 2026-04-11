from flask import Blueprint, redirect, request, jsonify
import urllib.parse, logging, json
from connectors.gcp_connector.auth.oauth import (
    get_auth_url,
    exchange_code_for_token,
)
from utils.auth.token_management import store_tokens_in_db
from connectors.gcp_connector.gcp_post_auth_tasks import gcp_post_auth_setup_task
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
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
@require_permission("connectors", "write")
def login(user_id):
    """Send Google OAuth login URL with user_id and org_id encoded in state parameter."""
    logging.info("Logging user in.")

    org_id = get_org_id_from_request()
    state_data = f"{user_id}|{org_id or ''}"
    state = urllib.parse.quote(state_data)
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
        state_data = urllib.parse.unquote(state)
        if '|' in state_data:
            user_id, org_id = state_data.split('|', 1)
            org_id = org_id or None
        else:
            user_id = state_data
            org_id = None
        from time import time  # local import to avoid global dependency loop

        token_data = exchange_code_for_token(code)
        if not token_data:
            logging.error("Failed to exchange OAuth code for tokens")
            return redirect(f"{FRONTEND_URL}?login=failed")

        token_data["expires_at"] = int(time()) + token_data.get("expires_in", 3600)

        store_tokens_in_db(user_id, token_data, "gcp")

        # Clear Redis cache to ensure new credentials are used immediately
        try:
            from utils.secrets.secret_cache import clear_secret_cache
            from utils.db.db_utils import connect_to_db_as_admin
            
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = 'gcp'",
                (user_id, org_id)
            )
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
        logging.error(f"Error during OAuth callback: {e}", exc_info=True)
        return redirect(f"{FRONTEND_URL}?login=gcp_failed")


@gcp_auth_bp.route("/gcp/setup/status/<task_id>", methods=["GET"])
@require_permission("connectors", "read")
def get_gcp_setup_status(user_id, task_id):
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
        logging.error(f"Error fetching task status: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch task status"}), 500


@gcp_auth_bp.route("/api/gcp/force-disconnect", methods=["POST"])
@require_permission("connectors", "write")
def force_disconnect_gcp(user_id):
    """Force disconnect GCP by deleting user tokens and clearing cache."""
    logging.info(f"Force disconnecting GCP for user {user_id}")
    
    org_id = get_org_id_from_request()
    conn = None
    cursor = None
    secret_ref = None
    
    try:
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = 'gcp'",
            (user_id, org_id)
        )
        result = cursor.fetchone()
        secret_ref = result[0] if result else None
        
        cursor.execute(
            "DELETE FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = 'gcp'",
            (user_id, org_id)
        )
        
        cursor.execute(
            "DELETE FROM user_preferences WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND preference_key = 'gcp_root_project'",
            (user_id, org_id)
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


@gcp_auth_bp.route("/api/gcp/service-account/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect_service_account(user_id):
    """Connect a GCP service account by uploading its JSON key.

    Stores the credential in the same `provider="gcp"` user_tokens slot used by
    the OAuth flow, discriminated by `auth_type="service_account"` inside the
    Vault-stored secret. Skips the Aurora per-user SA impersonation chain
    entirely — the uploaded key itself is the working identity.
    """
    # Lazy imports: scope google-api-python-client import failures to this
    # route so a version-drift breakage can't take out the whole gcp_auth_bp
    # (login/callback/force-disconnect must stay available).
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    from connectors.gcp_connector.gcp.projects import get_project_list

    payload = request.get_json(force=True, silent=True) or {}
    raw_sa_json = payload.get("service_account_json")

    if not raw_sa_json or not isinstance(raw_sa_json, str):
        return jsonify({
            "error": "Missing 'service_account_json' field or value is not a string"
        }), 400

    # 1. Parse the uploaded JSON text
    try:
        sa_info = json.loads(raw_sa_json)
    except json.JSONDecodeError as exc:
        logging.warning(
            "GCP SA connect: uploaded file is not valid JSON for user %s: %s",
            user_id,
            exc,
        )
        return jsonify({
            "error": f"Uploaded file is not valid JSON: {exc.msg}"
        }), 400

    if not isinstance(sa_info, dict):
        return jsonify({
            "error": "Service account JSON must be a JSON object"
        }), 400

    # Validate required fields
    if sa_info.get("type") != "service_account":
        return jsonify({
            "error": "Service account JSON must have 'type' field equal to 'service_account'"
        }), 400

    required_fields = ("project_id", "private_key", "client_email", "token_uri")
    for field in required_fields:
        if not sa_info.get(field):
            return jsonify({
                "error": f"Missing required field '{field}' in service account JSON"
            }), 400

    client_email = sa_info["client_email"]
    home_project_id = sa_info["project_id"]

    try:
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception as exc:
        logging.warning(
            "GCP SA connect: failed to build credentials (error_type=%s)",
            type(exc).__name__,
        )
        # Do not echo the raw Google exception back to the client — it can
        # contain SA identifiers, token URIs, or other internals.
        return jsonify({
            "error": "Service account key is malformed — check that 'private_key' is a valid PEM and all fields are intact"
        }), 400

    try:
        creds.refresh(Request())
    except Exception as exc:
        logging.warning(
            "GCP SA connect: credential refresh failed (error_type=%s)",
            type(exc).__name__,
        )
        # Do not echo the raw Google exception back to the client — it can
        # contain SA identifiers, token URIs, or other internals.
        return jsonify({
            "error": "Google rejected the service account key — it may be revoked, the SA may be disabled, or the project may not have the required APIs enabled"
        }), 400

    # Fallback rationale: the SA always has access to its home project even if
    # it cannot enumerate siblings (e.g. missing CRM API enablement or
    # resourcemanager.projects.list permission), so the SA's own project_id is
    # a safe minimum.
    home_fallback = [{"project_id": home_project_id, "name": home_project_id}]
    try:
        listed = get_project_list(creds)
        accessible_projects = [
            {"project_id": p.get("projectId"), "name": p.get("name") or p.get("projectId")}
            for p in listed
            if p.get("projectId")
        ] or home_fallback
        if accessible_projects is home_fallback:
            logging.warning(
                "GCP SA connect: projects.list returned empty — falling back to SA's embedded project_id"
            )
    except Exception as exc:
        logging.warning(
            "GCP SA connect: projects.list failed (error_type=%s) — falling back to SA's embedded project_id",
            type(exc).__name__,
        )
        accessible_projects = home_fallback

    # `email` is set so store_tokens_in_db populates the user_tokens.email
    # column the connected-accounts UI uses as a display label.
    from connectors.gcp_connector.auth import GCP_AUTH_TYPE_SA
    token_payload = {
        "auth_type": GCP_AUTH_TYPE_SA,
        "service_account_json": raw_sa_json,
        "client_email": client_email,
        "email": client_email,
        "default_project_id": home_project_id,
        "accessible_projects": accessible_projects,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "gcp")
        logging.info(
            "GCP SA connect: stored service account credentials for user %s (projects=%d)",
            user_id,
            len(accessible_projects),
        )
    except Exception as exc:
        logging.error(
            "GCP SA connect: failed to store credentials for user %s (error_type=%s)",
            user_id,
            type(exc).__name__,
        )
        return jsonify({"error": "Failed to store GCP service account credentials"}), 500

    # gcp_post_auth_setup_task is intentionally NOT fired: that task creates a
    # per-user Aurora SA and grants impersonation, which only applies to OAuth
    # mode. The uploaded SA key IS the working identity for SA mode.

    # Clear any OAuth-era preferences so stale Aurora-created SA metadata and
    # the previous root project don't leak into the SA-mode UI. The user can
    # set a new root project via the manage dialog after connecting.
    try:
        from utils.auth.stateless_auth import store_user_preference
        store_user_preference(user_id, "gcp_service_accounts", None)
        store_user_preference(user_id, "gcp_root_project", None)
    except Exception as exc:
        logging.warning(
            "GCP SA connect: failed to clear stale GCP preferences (error_type=%s)",
            type(exc).__name__,
        )

    # Clear Redis GCP caches for the user (mirrors /api/gcp/force-disconnect).
    org_id = get_org_id_from_request()
    conn = None
    cursor = None
    secret_ref_to_clear = None
    try:
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = 'gcp'",
            (user_id, org_id),
        )
        result = cursor.fetchone()
        if result and result[0]:
            secret_ref_to_clear = result[0]
    except Exception as exc:
        logging.warning(
            "GCP SA connect: failed to look up secret_ref for user %s (error_type=%s)",
            user_id,
            type(exc).__name__,
        )
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if secret_ref_to_clear:
        try:
            clear_secret_cache(secret_ref_to_clear)
            logging.info(
                "GCP SA connect: cleared Redis cache for user %s after SA upload", user_id,
            )
        except Exception as exc:
            logging.warning(
                "GCP SA connect: failed to clear Redis cache for user %s (error_type=%s)",
                user_id,
                type(exc).__name__,
            )

    # Also clear the per-user GCP setup cache (Redis + in-process env vars +
    # temp cred files). Critical on OAuth → SA switches so stale impersonation
    # env vars from a prior OAuth run can't leak into SA-mode gcloud calls.
    try:
        from chat.backend.agent.tools.auth.gcp_cached_auth import clear_gcp_cache_for_user
        clear_gcp_cache_for_user(user_id)
    except Exception as exc:
        logging.warning(
            "GCP SA connect: failed to clear gcp_cached_auth state for user %s (error_type=%s)",
            user_id,
            type(exc).__name__,
        )

    return jsonify({
        "success": True,
        "email": client_email,
        "default_project_id": home_project_id,
        "accessible_projects": accessible_projects,
    })


@gcp_auth_bp.route("/gcp/post-auth-retry", methods=["POST"])
@require_permission("connectors", "write")
def post_auth_retry(user_id):
    """Retry post-auth setup with selected projects."""
    try:
        data = request.get_json()
        selected_project_ids = data.get("selected_project_ids", [])
        
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
        logging.error(f"Error in post-auth retry: {e}", exc_info=True)
        return jsonify({"error": "Failed to retry post-auth setup"}), 500

