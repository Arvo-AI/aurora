"""
Bitbucket Cloud authentication routes.
Handles OAuth login, API token login, callback, status, and disconnect.
"""
import logging
import os
import time

from flask import Blueprint, jsonify, render_template, request

from utils.auth.stateless_auth import get_user_id_from_request, get_credentials_from_db
from utils.web.cors_utils import create_cors_response

bitbucket_bp = Blueprint("bitbucket", __name__)
logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


@bitbucket_bp.route("/login", methods=["POST", "OPTIONS"])
def bitbucket_login():
    """Handle Bitbucket login - either API token or OAuth initiation."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json() or {}
        user_id = data.get("userId") or get_user_id_from_request()

        if not user_id:
            return jsonify({"error": "User ID is required"}), 400

        api_token = data.get("api_token")
        email = data.get("email")

        if bool(api_token) != bool(email):
            return jsonify({"error": "Both email and API token are required for API token authentication"}), 400

        if api_token and email:
            # --- API token flow ---
            try:
                from connectors.bitbucket_connector.api_client import BitbucketAPIClient

                client = BitbucketAPIClient(
                    access_token=api_token,
                    auth_type="api_token",
                    email=email,
                )

                # Validate credentials by fetching user profile
                user_data = client.get_current_user()
                if not user_data or user_data.get("error"):
                    logger.error("Bitbucket API token validation failed")
                    if user_data and user_data.get("missing_scopes"):
                        missing = ", ".join(user_data["missing_scopes"])
                        return jsonify({
                            "error": f"Missing required scopes: {missing}. "
                                     "Please create a new API token that includes these scopes.",
                        }), 400
                    return jsonify({"error": "Invalid Bitbucket credentials. Check your email and API token."}), 400

                username = user_data.get("username")
                display_name = user_data.get("display_name")

                from utils.auth.token_management import store_tokens_in_db

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
                    "message": f"Successfully connected to Bitbucket as {username}",
                    "username": username,
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
                    "error": "Bitbucket OAuth is not configured",
                    "error_code": "BITBUCKET_NOT_CONFIGURED",
                    "message": "Bitbucket OAuth environment variables (BB_OAUTH_CLIENT_ID and BB_OAUTH_CLIENT_SECRET) are not configured.",
                }), 400

            from connectors.bitbucket_connector.oauth_utils import get_auth_url

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

        logger.info(f"Received Bitbucket code: {code[:5]}...")

        from connectors.bitbucket_connector.oauth_utils import exchange_code_for_token

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
        from connectors.bitbucket_connector.api_client import BitbucketAPIClient

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
            from connectors.bitbucket_connector.oauth_utils import validate_oauth_state
            user_id = validate_oauth_state(state)

        if not user_id:
            logger.error("Invalid or expired OAuth state token in Bitbucket callback")
            return render_template(
                "bitbucket_callback_error.html",
                error="Invalid or expired OAuth state. Please try connecting again.",
                frontend_url=FRONTEND_URL,
            )

        try:
            from utils.auth.token_management import store_tokens_in_db

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


@bitbucket_bp.route("/status", methods=["GET", "OPTIONS"])
def bitbucket_status():
    """Check Bitbucket connection status for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"connected": False, "error": "User ID required"}), 400

        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds or not bb_creds.get("access_token"):
            return jsonify({"connected": False})

        auth_type = bb_creds.get("auth_type", "oauth")

        # Auto-refresh OAuth tokens
        if auth_type == "oauth":
            from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed

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
        from connectors.bitbucket_connector.api_client import BitbucketAPIClient

        client = BitbucketAPIClient(
            access_token=bb_creds["access_token"],
            auth_type=auth_type,
            email=bb_creds.get("email"),
        )
        user_data = client.get_current_user()

        if not user_data or user_data.get("error"):
            return jsonify({"connected": False, "error": "Invalid or expired token"})

        return jsonify({
            "connected": True,
            "username": user_data.get("username"),
            "display_name": user_data.get("display_name"),
            "auth_type": auth_type,
        })

    except Exception as e:
        logger.error(f"Error checking Bitbucket status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check Bitbucket status"}), 500


@bitbucket_bp.route("/disconnect", methods=["POST", "OPTIONS"])
def bitbucket_disconnect():
    """Disconnect Bitbucket account for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        from utils.secrets.secret_ref_utils import delete_user_secret

        # Delete both bitbucket credentials and workspace selection
        delete_user_secret(user_id, "bitbucket")
        delete_user_secret(user_id, "bitbucket_workspace_selection")

        logger.info("Disconnected Bitbucket account")
        return jsonify({"success": True, "message": "Bitbucket account disconnected"})

    except Exception as e:
        logger.error(f"Error disconnecting Bitbucket: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Bitbucket"}), 500
