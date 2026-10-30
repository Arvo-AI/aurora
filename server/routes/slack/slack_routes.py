"""
Slack OAuth routes for Aurora Slack integration.
Handles OAuth flow, connection status, and disconnection.
"""

import logging
import os
import time
from urllib.parse import quote
from flask import Blueprint, request, jsonify, redirect
import requests
from connectors.slack_connector.oauth import get_auth_url, exchange_code_for_token
from connectors.slack_connector.client import create_incidents_channel, join_existing_incidents_channel
from utils.auth.stateless_auth import (
    get_credentials_from_db,
    get_org_id_for_user,
    get_org_preference,
    store_org_preference,
)
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.auth.token_management import store_tokens_in_db
from utils.auth.rbac_decorators import require_permission

slack_bp = Blueprint("slack", __name__)

# Get frontend URL from environment
FRONTEND_URL = os.getenv("FRONTEND_URL")


@slack_bp.route("/", methods=["GET"], strict_slashes=False)
@require_permission("connectors", "read")
def slack_status(user_id):
    """GET /slack - Get connection status"""
    try:
        slack_creds = get_credentials_from_db(user_id, "slack")
        if not slack_creds or not slack_creds.get("access_token"):
            return jsonify({"connected": False})
        
        # Validate the stored token by calling auth.test
        headers = {"Authorization": f"Bearer {slack_creds['access_token']}"}
        test_response = requests.post(
            "https://slack.com/api/auth.test",
            headers=headers,
            timeout=20
        )
        
        if test_response.status_code == 200:
            test_data = test_response.json()
            if test_data.get('ok', False):
                return jsonify({
                    "connected": True,
                    "team_name": test_data.get('team', slack_creds.get('team_name')),
                    "user_name": test_data.get('user'),
                    "team_id": test_data.get('team_id', slack_creds.get('team_id')),
                    "team_url": test_data.get('url', slack_creds.get('team_url')),
                    "connected_at": slack_creds.get('connected_at'),
                    "incidents_channel_name": slack_creds.get('incidents_channel_name'),
                    "incidents_channel_id": slack_creds.get('incidents_channel_id'),
                    "app_id": slack_creds.get('app_id'),
                })
        
        # Token is invalid
        return jsonify({"connected": False, "error": "Invalid or expired token"})
    
    except Exception as e:
        logging.error(f"Error checking Slack status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check Slack status"}), 500


@slack_bp.route("/", methods=["POST"], strict_slashes=False)
@require_permission("connectors", "write")
def slack_connect(user_id):
    """POST /slack - Initiate OAuth connection (returns oauth_url)"""
    try:
        # Generate OAuth authorization URL
        oauth_url = get_auth_url(state=user_id)
        return jsonify({
            "oauth_url": oauth_url,
            "message": "Redirect to Slack for authentication"
        })
    except Exception as e:
        logging.error(f"Error initiating Slack OAuth: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate Slack OAuth"}), 500


@slack_bp.route("/demo", methods=["POST"])
@require_permission("connectors", "read")
def slack_demo(user_id):
    """POST /slack/demo - Play the sample incident in the caller's Slack DM."""
    try:
        from routes.slack.slack_onboarding import run_demo_incident_task
        from utils.db.connection_pool import db_pool

        creds = get_credentials_from_db(user_id, "slack") or {}
        team_id = creds.get("team_id")
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT slack_team_id, slack_user_id FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        # The caller's own Slack identity, else the installer's (same workspace).
        if row and row[1] and (not team_id or row[0] == team_id):
            team_id, slack_user_id = row[0], row[1]
        elif creds.get("user_id"):
            slack_user_id = creds["user_id"]
        else:
            return jsonify({"error": "Slack is not connected"}), 409
        if not team_id:
            return jsonify({"error": "Slack is not connected"}), 409
        run_demo_incident_task.delay(team_id, slack_user_id, None)
        return jsonify({"started": True})
    except Exception:
        logging.exception("Error starting the Slack demo")
        return jsonify({"error": "Failed to start the demo"}), 500


@slack_bp.route("/", methods=["DELETE"], strict_slashes=False)
@require_permission("connectors", "write")
def slack_disconnect(user_id):
    """DELETE /slack - Disconnect Slack workspace"""
    try:        
        delete_success = delete_user_secret(user_id, "slack")
        
        if delete_success:
            logging.info(f"Disconnected Slack for user {user_id}")
            return jsonify({"success": True, "message": "Slack workspace disconnected"})
        else:
            logging.error(f"Failed to disconnect Slack for user {user_id}")
            return jsonify({"error": "Failed to disconnect Slack workspace"}), 500
    
    except Exception as e:
        logging.error(f"Error disconnecting Slack: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Slack"}), 500


def complete_slack_connection(user_id, token_data):
    """Store a freshly exchanged Slack install against ``user_id``: incidents
    channel, credentials, Slack memory and channel auto-registration.

    Shared by the in-app connect callback and the website's one-click signup.
    Returns None on success, else a short error code (or user-facing hint)
    for the redirect.
    """
    access_token = token_data.get('access_token')
    team_info = token_data.get('team', {})
    authed_user = token_data.get('authed_user', {})

    if not access_token:
        logging.error(f"No access token in Slack response (keys: {list(token_data.keys())})")
        return "no_token"

    # Create incidents channel first — connection is only valid with a working channel
    installer_slack_user_id = authed_user.get('id')
    team_name = team_info.get('name', 'Unknown')

    # On reconnect, reuse the previously-stored channel from org preferences
    # (survives disconnect since it's stored separately from OAuth credentials)
    channel_result = None
    org_id = get_org_id_for_user(user_id)
    existing_channel_id = None
    if org_id:
        # Strict: guessing "no saved channel" on a failed read would create a
        # second incidents channel and orphan the saved one.
        try:
            existing_channel_id = get_org_preference(org_id, 'slack_incidents_channel_id', strict=True)
        except Exception:
            logging.exception("Failed to read the saved Slack incidents channel")
            return "Aurora could not read your saved Slack settings. Please retry."

    if existing_channel_id:
        channel_result = join_existing_incidents_channel(access_token, existing_channel_id)
        if not channel_result.get('ok'):
            channel_result = None

    if not channel_result:
        channel_result = create_incidents_channel(access_token, team_name, installer_slack_user_id)
    if not channel_result.get('ok'):
        error_msg = channel_result.get('error', 'Unknown error')
        logging.error(f"Failed to create incidents channel: {error_msg}")
        return (
            "Aurora could not create an incidents channel in your workspace. "
            "Please check the bot's permissions and retry."
        )

    # Store the token in the database (including channel info)
    try:
        slack_token_data = {
            "access_token": access_token,
            "team_name": team_name,
            "team_id": team_info.get('id'),
            "user_id": authed_user.get('id'),
            # Bot's own Slack user id — used to recognise Aurora's own joins in
            # member_joined_channel events (so we auto-register channels Aurora
            # is invited to, e.g. incident.io-created ones).
            "bot_user_id": token_data.get('bot_user_id'),
            # Lets the app deep-link straight to Aurora inside Slack.
            "app_id": token_data.get('app_id'),
            "connected_at": int(time.time()),
            "incidents_channel_id": channel_result.get('channel_id'),
            "incidents_channel_name": channel_result.get('channel_name'),
        }
        store_tokens_in_db(user_id, slack_token_data, "slack")

        # Persist channel ID as org preference so it survives disconnect/reconnect
        if org_id and channel_result.get('channel_id'):
            store_org_preference(org_id, 'slack_incidents_channel_id', channel_result['channel_id'])
            store_org_preference(org_id, 'slack_incidents_channel_name', channel_result.get('channel_name', ''))

        # Seed the default "Slack" memory (teammate policy) so Aurora starts
        # with sensible behaviour. Non-destructive: skips if one already
        # exists, so user/agent edits survive reconnects. Best-effort — a
        # seeding failure must not fail the connection.
        try:
            from services.memory.slack_memory import seed_slack_memory
            seed_slack_memory(user_id)
        except Exception:
            logging.warning("Failed to seed Slack memory (non-fatal)", exc_info=True)

        # Auto-register the workspace's channels (all, capped to the 50 most
        # recent) so Aurora is immediately aware of them and generates cheap
        # descriptions. Enqueued as a background task — a large workspace's
        # paginated Slack I/O + DB writes can exceed the OAuth callback's
        # request budget and time out the redirect. Best-effort dispatch;
        # never fail the connection over this (the manage page can Refresh).
        try:
            from routes.slack.slack_channel_metadata import auto_register_channels_task
            auto_register_channels_task.delay(user_id, team_info.get('id'))
        except Exception:
            logging.warning("Failed to enqueue Slack channel auto-registration (non-fatal)", exc_info=True)

        # First connection for this org: welcome the installer in a DM with
        # the demo and the setup checklist. Reconnects stay quiet.
        if not existing_channel_id:
            try:
                from routes.slack.slack_onboarding import send_welcome_dm_task
                send_welcome_dm_task.delay(user_id, installer_slack_user_id, channel_result.get('channel_id'))
            except Exception:
                logging.warning("Failed to enqueue the Slack welcome DM (non-fatal)", exc_info=True)

        logging.info("Incidents channel ready, Slack credentials stored successfully")
    except Exception:
        logging.exception("Failed to store Slack credentials")
        return "storage_failed"
    return None


@slack_bp.route("/callback", methods=["GET", "POST"])
def slack_callback():
    """Handle the OAuth callback from Slack."""
    try:
        # Get the authorization code from query parameters
        code = request.args.get("code")
        state = request.args.get("state")  # Contains user_id

        # Website one-click installs and "Sign in with Slack" share this
        # redirect URL; their state is a signed token that a bare user id can
        # never verify as.
        if state:
            from routes.slack.slack_signin import handle_signin_callback, load_signin_state
            from routes.slack.slack_signup import handle_signup_callback, load_signup_state
            signup_state = load_signup_state(state)
            if signup_state is not None:
                return handle_signup_callback(code, signup_state, request.args.get("error"))
            signin_state = load_signin_state(state)
            if signin_state is not None:
                return handle_signin_callback(code, signin_state)

        if not code or not state:
            logging.error("No code or state provided in Slack callback")
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=no_code_or_state")
        
        user_id = state
        
        # Exchange code for token
        try:
            token_data = exchange_code_for_token(code)
        except Exception as e:
            logging.error(f"Token exchange failed: {e}", exc_info=True)
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=token_exchange_failed")

        error = complete_slack_connection(user_id, token_data)
        if error:
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error={quote(error)}")
        
        # Redirect to frontend with success (frontend reads team name from status endpoint)
        return redirect(f"{FRONTEND_URL}/slack/manage?slack_auth=success")
    
    except Exception as e:
        logging.error("Error during Slack callback", exc_info=True)
        return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=unexpected_error")
