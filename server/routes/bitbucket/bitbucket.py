"""
Bitbucket Cloud authentication routes.
Handles OAuth login, API token login, callback, status, and disconnect.
"""
import logging
import os
import time

from flask import Blueprint, jsonify, render_template, request

from utils.auth.stateless_auth import get_credentials_from_db
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.db.connection_pool import db_pool

_REQUIRED_SCOPES = {
    "read:user:bitbucket", "read:workspace:bitbucket", "read:project:bitbucket",
    "read:repository:bitbucket", "write:repository:bitbucket",
    "read:pullrequest:bitbucket", "write:pullrequest:bitbucket",
    "read:issue:bitbucket", "write:issue:bitbucket",
    "read:pipeline:bitbucket", "write:pipeline:bitbucket",
}
from utils.auth.stateless_auth import set_rls_context
from connectors.bitbucket_connector.api_client import BitbucketAPIClient
from connectors.bitbucket_connector.oauth_utils import exchange_code_for_token, get_auth_url, validate_oauth_state, refresh_token_if_needed
from utils.auth.token_management import store_tokens_in_db

bitbucket_bp = Blueprint("bitbucket", __name__)
logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


@bitbucket_bp.route("/login", methods=["POST"])
@require_permission("connectors", "write")
def bitbucket_login(user_id):
    """Handle Bitbucket login - either API token or OAuth initiation."""
    try:
        data = request.get_json() or {}

        api_token = data.get("api_token")
        email = data.get("email")

        if bool(api_token) != bool(email):
            return jsonify({"error": "Both email and API token are required for API token authentication"}), 400

        if api_token and email:
            # --- API token flow ---
            try:
                client = BitbucketAPIClient(
                    access_token=api_token,
                    auth_type="api_token",
                    email=email,
                )

                # Validate credentials by fetching user profile
                user_data = client.get_current_user()
                if not user_data or user_data.get("error"):
                    status_code = user_data.get("status") if user_data else None
                    error_msg = user_data.get("message", "") if user_data else ""
                    logger.error(f"Bitbucket API token validation failed: status={status_code} message={error_msg}")

                    if user_data and user_data.get("missing_scopes"):
                        missing = ", ".join(user_data["missing_scopes"])
                        return jsonify({
                            "error": f"Missing required scopes: {missing}. "
                                     "Please create a new API token that includes these scopes.",
                        }), 400

                    if status_code in (401, 403):
                        return jsonify({
                            "error": "Authentication failed. Either your email/token is incorrect, "
                                     "or you are using a classic API token (not supported). "
                                     "Create a scoped token at id.atlassian.com/manage-profile/security/api-tokens"
                        }), 400

                    return jsonify({"error": "Invalid Bitbucket credentials. Check your email and API token."}), 400

                username = user_data.get("username")
                display_name = user_data.get("display_name")

                # Validate token has all required scopes (read from same /user response)
                granted = set(user_data.get("_granted_scopes", []))
                missing = _REQUIRED_SCOPES - granted
                if missing:
                    logger.warning(f"Bitbucket token for {sanitize(email)} missing scopes: {missing}")

                token_data = {
                    "access_token": api_token,
                    "auth_type": "api_token",
                    "email": email,
                    "username": username,
                    "display_name": display_name,
                }

                store_tokens_in_db(user_id, token_data, "bitbucket")
                logger.info("Stored Bitbucket API token credentials")

                return jsonify({
                    "success": True,
                    "username": username,
                    "display_name": display_name,
                    "auth_type": "api_token",
                    **({"missing_scopes": sorted(missing)} if missing else {}),
                })

            except Exception as e:
                logger.error(f"Error storing Bitbucket API token: {e}", exc_info=True)
                return jsonify({"error": "Failed to store Bitbucket credentials"}), 500
        else:
            # --- OAuth flow ---
            client_id = os.getenv("BB_OAUTH_CLIENT_ID")
            client_secret = os.getenv("BB_OAUTH_CLIENT_SECRET")

            if not client_id or not client_secret:
                logger.error("Bitbucket OAuth client ID or secret not configured")
                return jsonify({
                    "error": "Bitbucket OAuth is not available. Use an API token instead.",
                    "error_code": "OAUTH_NOT_CONFIGURED",
                }), 400

            oauth_url = get_auth_url(user_id)

            return jsonify({
                "oauth_url": oauth_url,
                "message": "Redirect to Bitbucket for authentication",
            })

    except Exception as e:
        logger.error(f"Error in Bitbucket login: {e}", exc_info=True)
        return jsonify({"error": "Failed to process Bitbucket login"}), 500


@bitbucket_bp.route("/callback", methods=["GET", "POST"])
def bitbucket_callback():
    """Handle the OAuth callback from Bitbucket."""
    try:
        code = request.args.get("code")
        if not code:
            logger.error("No code provided in Bitbucket callback")
            return render_template(
                "bitbucket_callback_error.html",
                error="No authorization code provided",
                frontend_url=FRONTEND_URL,
            )

        logger.info(f"Received Bitbucket code: {sanitize(code)[:5]}...")

        token_response = exchange_code_for_token(code)
        if not token_response:
            return render_template(
                "bitbucket_callback_error.html",
                error="Failed to authenticate with Bitbucket",
                frontend_url=FRONTEND_URL,
            )

        access_token = token_response.get("access_token")
        if not access_token:
            logger.error(f"No access token in Bitbucket response: {list(token_response.keys())}")
            return render_template(
                "bitbucket_callback_error.html",
                error="Invalid response from Bitbucket",
                frontend_url=FRONTEND_URL,
            )

        # Fetch user info using the API client
        client = BitbucketAPIClient(access_token=access_token)
        user_data = client.get_current_user()

        if not user_data:
            return render_template(
                "bitbucket_callback_error.html",
                error="Failed to get user information",
                frontend_url=FRONTEND_URL,
            )

        username = user_data.get("username")
        display_name = user_data.get("display_name")

        logger.info(f"Authenticated as Bitbucket user: {username}")

        # Calculate token expiry
        expires_in = token_response.get("expires_in", 7200)
        expires_at = time.time() + expires_in

        # Validate CSRF state and extract user_id
        state = request.args.get("state")
        user_id = None
        if state:
            user_id = validate_oauth_state(state)

        if not user_id:
            logger.error("Invalid or expired OAuth state token in Bitbucket callback")
            return render_template(
                "bitbucket_callback_error.html",
                error="Invalid or expired OAuth state. Please try connecting again.",
                frontend_url=FRONTEND_URL,
            )

        try:
            bb_token_data = {
                "access_token": access_token,
                "refresh_token": token_response.get("refresh_token"),
                "expires_at": expires_at,
                "auth_type": "oauth",
                "username": username,
                "display_name": display_name,
            }

            store_tokens_in_db(user_id, bb_token_data, "bitbucket")
            logger.info("Stored Bitbucket OAuth credentials")
        except Exception as e:
            logger.error(f"Failed to store Bitbucket credentials: {e}", exc_info=True)
            return render_template(
                "bitbucket_callback_error.html",
                error="Authentication succeeded but failed to save credentials. Please try again.",
                frontend_url=FRONTEND_URL,
            )

        try:
            from utils.auth.tool_registry import seed_org_tool_permissions
            from utils.auth.stateless_auth import get_org_id_for_user
            org_id = get_org_id_for_user(user_id)
            if org_id:
                seed_org_tool_permissions(org_id, user_id)
        except Exception:
            logger.warning("[BITBUCKET-CALLBACK] failed to seed tool permissions", exc_info=True)

        return render_template(
            "bitbucket_callback_success.html",
            bitbucket_username=username,
            frontend_url=FRONTEND_URL,
        )

    except Exception as e:
        logger.error(f"Error during Bitbucket callback: {e}", exc_info=True)
        return render_template(
            "bitbucket_callback_error.html",
            error="An unexpected error occurred during Bitbucket authentication",
            frontend_url=FRONTEND_URL,
        )


@bitbucket_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def bitbucket_status(user_id):
    """Check Bitbucket connection status for a user."""
    try:
        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds or not bb_creds.get("access_token"):
            return jsonify({"connected": False})

        auth_type = bb_creds.get("auth_type", "oauth")

        # Auto-refresh OAuth tokens
        if auth_type == "oauth":
            old_access_token = bb_creds.get("access_token")
            bb_creds = refresh_token_if_needed(bb_creds)

            # Persist if the access token was refreshed
            if bb_creds.get("access_token") != old_access_token:
                try:
                    from utils.auth.token_management import store_tokens_in_db
                    store_tokens_in_db(user_id, bb_creds, "bitbucket")
                except Exception as e:
                    logger.warning(f"Failed to persist refreshed Bitbucket token: {e}")

        # Validate by making an API call
        client = BitbucketAPIClient(
            access_token=bb_creds["access_token"],
            auth_type=auth_type,
            email=bb_creds.get("email"),
        )
        user_data = client.get_current_user()

        if not user_data or user_data.get("error"):
            return jsonify({"connected": False, "error": "Invalid or expired token"})

        # Check for missing scopes (piggybacks on the /user call we just made)
        granted = set(user_data.get("_granted_scopes", []))
        missing = _REQUIRED_SCOPES - granted if granted else set()

        return jsonify({
            "connected": True,
            "username": user_data.get("username"),
            "display_name": user_data.get("display_name"),
            "auth_type": auth_type,
            **({"missing_scopes": sorted(missing)} if missing else {}),
        })

    except Exception as e:
        logger.error(f"Error checking Bitbucket status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check Bitbucket status"}), 500


@bitbucket_bp.route("/disconnect", methods=["POST"])
@require_permission("connectors", "write")
def bitbucket_disconnect(user_id):
    """Disconnect Bitbucket account for a user."""
    try:
        # Delete both bitbucket credentials and workspace selection
        delete_user_secret(user_id, "bitbucket")

        # Also clean up legacy vault entry if it exists
        try:
            delete_user_secret(user_id, "bitbucket_workspace_selection")
        except Exception as e:
            logger.debug(f"Legacy Bitbucket workspace selection cleanup skipped: {e}")

        # Clear connected repos
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[Bitbucket:disconnect]")
                    cur.execute(
                        "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'bitbucket'",
                        (user_id,),
                    )
                    conn.commit()
        except Exception as e:
            logger.warning(f"Failed to clear connected_repos on disconnect: {e}")

        logger.info("Disconnected Bitbucket account")
        return jsonify({"success": True, "message": "Bitbucket account disconnected"})

    except Exception as e:
        logger.error(f"Error disconnecting Bitbucket: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Bitbucket"}), 500
