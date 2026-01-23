"""
Slack OAuth routes for Aurora Slack integration.
Handles OAuth flow, connection status, and disconnection.
"""

import logging
import os
import traceback
import time
from flask import Blueprint, request, jsonify, redirect
import requests
from utils.auth.stateless_auth import get_user_id_from_request
from connectors.slack_connector.oauth import get_auth_url, exchange_code_for_token
from connectors.slack_connector.client import create_incidents_channel, get_slack_client_for_user
from utils.auth.stateless_auth import get_credentials_from_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.auth.token_management import store_tokens_in_db

slack_bp = Blueprint("slack", __name__)

# Get frontend URL from environment
FRONTEND_URL = os.getenv("FRONTEND_URL")


@slack_bp.route("/", methods=["GET", "POST", "DELETE"], strict_slashes=False)
def slack_connection():
    """
    Unified RESTful endpoint for Slack connection management.
    
    GET /slack - Get connection status
    POST /slack - Initiate OAuth connection (returns oauth_url)
    DELETE /slack - Disconnect Slack workspace
    """
    # Get user_id from authenticated session
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User ID required"}), 400
    
    method = request.method
    
    if method == "GET":
        # GET /slack - Check connection status
        try:
            slack_creds = get_credentials_from_db(user_id, "slack")
            if not slack_creds or not slack_creds.get("access_token"):
                return jsonify({"connected": False})
            
            # Validate the stored token by calling auth.test
            headers = {"Authorization": f"Bearer {slack_creds['access_token']}"}
            test_response = requests.post(
                "https://slack.com/api/auth.test",
                headers=headers
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
                    })
            
            # Token is invalid
            return jsonify({"connected": False, "error": "Invalid or expired token"})
        
        except Exception as e:
            logging.error(f"Error checking Slack status: {e}", exc_info=True)
            return jsonify({"connected": False, "error": str(e)}), 500
    
    elif method == "POST":
        # POST /slack - Initiate OAuth connection
        try:
            # Generate OAuth authorization URL
            oauth_url = get_auth_url(state=user_id)
            return jsonify({
                "oauth_url": oauth_url,
                "message": "Redirect to Slack for authentication"
            })
        except Exception as e:
            logging.error(f"Error initiating Slack OAuth: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    
    elif method == "DELETE":
        # DELETE /slack - Disconnect
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
            return jsonify({"error": str(e)}), 500
    
    else:
        return jsonify({"error": "Method not allowed"}), 405


@slack_bp.route("/callback", methods=["GET", "POST"])
def slack_callback():
    """Handle the OAuth callback from Slack."""
    try:
        # Get the authorization code from query parameters
        code = request.args.get("code")
        state = request.args.get("state")  # Contains user_id
        
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
        
        # Extract token information
        access_token = token_data.get('access_token')
        team_info = token_data.get('team', {})
        authed_user = token_data.get('authed_user', {})
        
        if not access_token:
            logging.error(f"No access token in Slack response: {token_data}")
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=no_token")
        
        # Create incidents channel first (blocking - OAuth fails if channel can't be created)
        installer_slack_user_id = authed_user.get('id')
        channel_result = create_incidents_channel(
            access_token, 
            team_info.get('name', 'Unknown'),
            installer_slack_user_id
        )
        if not channel_result.get('ok'):
            error_msg = channel_result.get('error', 'Unknown error')
            logging.error(f"Failed to create incidents channel: {error_msg}")
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=channel_creation_failed")
        
        # Store the token in the database (including channel info)
        try:
            slack_token_data = {
                "access_token": access_token,
                "team_name": team_info.get('name', 'Unknown'),
                "team_id": team_info.get('id'),
                "user_id": authed_user.get('id'),
                "connected_at": int(time.time()),
                "incidents_channel_id": channel_result.get('channel_id'),
                "incidents_channel_name": channel_result.get('channel_name'),
            }
            
            store_tokens_in_db(user_id, slack_token_data, "slack")
            logging.info(f"Incidents channel ready in {team_info.get('name')} workspace, channel: #{channel_result.get('channel_name')}")
            
        except Exception as e:
            logging.error(f"Failed to store Slack credentials: {e}", exc_info=True)
            return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=storage_failed")
        
        # Redirect to frontend with success
        return redirect(f"{FRONTEND_URL}?slack_auth=success&team={team_info.get('name', 'Unknown')}")
    
    except Exception as e:
        logging.error(f"Error during Slack callback: {e}", exc_info=True)
        return redirect(f"{FRONTEND_URL}?slack_auth=failed&error=unexpected_error")


@slack_bp.route("/channels", methods=["GET"])
def list_channels():
    """List Slack channels the bot can see (may include channels the bot is not a member of)."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User ID required"}), 400
    
    try:
        client = get_slack_client_for_user(user_id)
        if not client:
            return jsonify({"error": "Slack not connected"}), 401
        
        channels = client.list_channels()
        
        # Format channel list for frontend
        formatted_channels = [{
            "id": ch.get("id"),
            "name": ch.get("name"),
            "is_member": ch.get("is_member"),
            "is_private": ch.get("is_private"),
            "num_members": ch.get("num_members")
        } for ch in channels]
        
        return jsonify({"channels": formatted_channels})
        
    except Exception as e:
        logging.error(f"Error listing Slack channels: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

