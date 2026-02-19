"""
Tailscale API Routes - Authentication and Tailnets

Provides endpoints for:
1. Connecting Tailscale account (OAuth client credentials)
2. Fetching tailnets (like projects in other providers)
3. Connection status and disconnect
4. Token refresh

Security:
- OAuth credentials are stored in HashiCorp Vault (not in database)
- Only secret references are stored in database
- Rate limiting applied to prevent brute force
- Input validation on all user-provided data
"""

import logging
import json
from flask import request, jsonify
from routes.tailscale import tailscale_bp
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import store_tokens_in_db, get_token_data
from utils.secrets.secret_ref_utils import has_user_credentials, delete_user_secret
from utils.db.connection_utils import set_connection_status
from utils.web.limiter_ext import limiter
from utils.logging.secure_logging import mask_credential_value
from utils.ssh.ssh_key_utils import generate_ssh_key_pair
from connectors.tailscale_connector.auth import (
    validate_tailscale_credentials,
    get_user_tailnets,
    get_valid_access_token,
    refresh_oauth_token
)
from connectors.tailscale_connector.api_client import create_reusable_auth_key_for_aurora

logger = logging.getLogger(__name__)


@tailscale_bp.route('/tailscale/connect', methods=['POST'])
@limiter.limit("10 per minute;50 per hour")
def tailscale_connect():
    """
    Connect Tailscale account using OAuth client credentials.

    Request body:
    {
        "clientId": "OAuth client ID",
        "clientSecret": "OAuth client secret",
        "tailnet": "optional tailnet name (uses default if not provided)"
    }
    """
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        data = request.get_json() or {}
        client_id = data.get('clientId') or data.get('client_id')
        client_secret = data.get('clientSecret') or data.get('client_secret')
        tailnet = data.get('tailnet')

        # Input validation
        if not client_id or not client_secret:
            return jsonify({"error": "Client ID and client secret are required"}), 400

        # Basic format validation
        client_id = client_id.strip()
        client_secret = client_secret.strip()

        if len(client_id) < 10:
            return jsonify({"error": "Invalid client ID format"}), 400

        if len(client_secret) < 10:
            return jsonify({"error": "Invalid client secret format"}), 400

        # Log with masked credentials for security
        logger.info(f"Tailscale connect attempt for user {user_id}, client_id: {mask_credential_value(client_id)}")

        # Validate credentials with Tailscale API
        success, account_info, error = validate_tailscale_credentials(
            client_id, client_secret, tailnet
        )

        if not success:
            logger.warning(f"Tailscale credential validation failed for user {user_id}: {error}")
            return jsonify({"error": error}), 401

        # Get access token for API calls
        access_token = account_info.get("token_data", {}).get("access_token")
        tailnet_name = account_info.get("tailnet") or "-"

        # Generate SSH key pair for this user
        ssh_private_key, ssh_public_key = generate_ssh_key_pair()

        # Create reusable auth key for Aurora to join the tailnet
        auth_key = None
        if access_token:
            key_success, auth_key, key_error = create_reusable_auth_key_for_aurora(
                access_token=access_token,
                tailnet=tailnet_name
            )
            if not key_success:
                logger.warning(f"Failed to create auth key for user {user_id}: {key_error}")
                # Continue without auth key - SSH will still work if device is accessible

        # Store credentials securely (credentials go to Vault)
        token_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "tailnet": account_info.get("tailnet"),
            "tailnet_name": account_info.get("tailnet_name"),
            "token_data": account_info.get("token_data"),
            "ssh_private_key": ssh_private_key,
            "ssh_public_key": ssh_public_key,
            "tailscale_auth_key": auth_key,
        }

        store_tokens_in_db(user_id, token_data, "tailscale")
        set_connection_status(user_id, "tailscale", client_id, "connected")
        logger.info(f"Tailscale connected for user {user_id}")

        return jsonify({
            "success": True,
            "message": "Tailscale connected successfully",
            "tailnet": account_info.get("tailnet"),
            "tailnetName": account_info.get("tailnet_name"),
            "deviceCount": account_info.get("device_count", 0)
        })

    except Exception as e:
        logger.error(f"Error connecting Tailscale for user: {e}", exc_info=True)
        return jsonify({"error": "Failed to connect Tailscale"}), 500


@tailscale_bp.route('/tailscale/tailnets', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
def tailscale_tailnets():
    """
    Fetch or save Tailscale tailnets (equivalent to projects).

    GET Returns:
    {
        "tailnets": [
            {"id": "...", "name": "...", "deviceCount": 0, "enabled": true}
        ]
    }

    POST Body:
    {
        "tailnets": [{"id": "...", "enabled": true/false}]
    }
    """
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        # Get stored credentials
        token_data = get_token_data(user_id, "tailscale")
        if not token_data:
            return jsonify({
                "error": "Tailscale not connected. Please connect your account.",
                "action": "CONNECT_REQUIRED"
            }), 401

        if request.method == 'POST':
            # Save tailnet selections
            data = request.get_json()
            tailnets = data.get("tailnets", [])

            # Store tailnet selections in user preferences
            from utils.db.db_utils import connect_to_db_as_admin
            conn = connect_to_db_as_admin()
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_preferences (user_id, preference_key, preference_value)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id, preference_key)
                        DO UPDATE SET preference_value = EXCLUDED.preference_value, updated_at = NOW()
                    """, (user_id, 'tailscale_tailnets', json.dumps(tailnets)))
                conn.commit()
            finally:
                conn.close()

            logger.info(f"Saved Tailscale tailnet selections for user {user_id}")
            return jsonify({"success": True, "message": "Tailnets saved"})

        # GET - Fetch tailnets
        client_id = token_data.get("client_id")
        client_secret = token_data.get("client_secret")

        if not client_id or not client_secret:
            return jsonify({"error": "Invalid stored credentials"}), 401

        # Get valid access token (refreshes if needed)
        success, access_token, error = get_valid_access_token(
            client_id, client_secret, token_data.get("token_data")
        )

        if not success:
            return jsonify({"error": error or "Failed to get access token"}), 401

        # Fetch tailnets
        success, tailnets, error = get_user_tailnets(access_token)

        if not success:
            return jsonify({"error": error}), 400

        # Load saved tailnet selections
        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        saved_selections = {}
        root_tailnet_id = None
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT preference_value FROM user_preferences
                    WHERE user_id = %s AND preference_key = %s
                """, (user_id, 'tailscale_tailnets'))
                result = cur.fetchone()
                if result and result[0]:
                    saved_data = json.loads(result[0]) if isinstance(result[0], str) else result[0]
                    for t in saved_data:
                        saved_selections[t.get('id')] = t.get('enabled', True)

                # Get root tailnet
                cur.execute("""
                    SELECT preference_value FROM user_preferences
                    WHERE user_id = %s AND preference_key = %s
                """, (user_id, 'tailscale_root_tailnet'))
                root_result = cur.fetchone()
                if root_result and root_result[0]:
                    root_tailnet_id = root_result[0]
        finally:
            conn.close()

        # Merge saved selections with fetched tailnets
        for tailnet in tailnets:
            tailnet_id = tailnet.get('id')
            if tailnet_id in saved_selections:
                tailnet['enabled'] = saved_selections[tailnet_id]
            else:
                tailnet['enabled'] = True  # Default to enabled
            tailnet['isRootTailnet'] = (tailnet_id == root_tailnet_id)

        return jsonify({"tailnets": tailnets})

    except Exception as e:
        logger.error(f"Error with Tailscale tailnets: {e}", exc_info=True)
        return jsonify({"error": "Failed to process tailnets request"}), 500


@tailscale_bp.route('/tailscale/status', methods=['GET'])
@limiter.limit("60 per minute")
def tailscale_status():
    """Check Tailscale connection status."""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"connected": False}), 200

        has_creds = has_user_credentials(user_id, "tailscale")

        # Get additional info if connected
        response = {
            "connected": has_creds,
            "provider": "tailscale"
        }

        if has_creds:
            token_data = get_token_data(user_id, "tailscale")
            if token_data:
                response["tailnet"] = token_data.get("tailnet")
                response["tailnetName"] = token_data.get("tailnet_name")

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error checking Tailscale status: {e}", exc_info=True)
        return jsonify({"connected": False}), 200


@tailscale_bp.route('/tailscale/disconnect', methods=['POST'])
@limiter.limit("10 per minute")
def tailscale_disconnect():
    """Disconnect Tailscale account."""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        # Get client_id before deleting for status update
        token_data = get_token_data(user_id, "tailscale")
        client_id = token_data.get("client_id", "unknown") if token_data else "unknown"

        # Delete stored credentials
        delete_user_secret(user_id, "tailscale")
        set_connection_status(user_id, "tailscale", client_id, "disconnected")

        # Also clear tailnet preferences
        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM user_preferences
                    WHERE user_id = %s AND preference_key IN (%s, %s)
                """, (user_id, 'tailscale_tailnets', 'tailscale_root_tailnet'))
            conn.commit()
        finally:
            conn.close()

        logger.info(f"Tailscale disconnected for user {user_id}")

        return jsonify({
            "success": True,
            "message": "Tailscale disconnected successfully"
        })

    except Exception as e:
        logger.error(f"Error disconnecting Tailscale: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Tailscale"}), 500


@tailscale_bp.route('/tailscale/ssh-setup', methods=['GET'])
@limiter.limit("30 per minute")
def tailscale_ssh_setup():
    """
    Get SSH setup instructions and public key for Aurora SSH access.

    Returns the user's SSH public key that needs to be added to target devices,
    along with step-by-step instructions.
    """
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        # Get stored token data
        token_data = get_token_data(user_id, "tailscale")
        if not token_data:
            return jsonify({
                "error": "Tailscale not connected. Please connect your account first.",
                "action": "CONNECT_REQUIRED"
            }), 401

        ssh_public_key = token_data.get("ssh_public_key")
        ssh_private_key = token_data.get("ssh_private_key")

        if not ssh_public_key or not ssh_private_key:
            # Generate keys if they don't exist (for users who connected before this feature)
            ssh_private_key, ssh_public_key = generate_ssh_key_pair()
            token_data["ssh_private_key"] = ssh_private_key
            token_data["ssh_public_key"] = ssh_public_key

            # Store updated token data (handles secret storage and cache clearing internally)
            store_tokens_in_db(user_id, token_data, "tailscale")

        tailnet_name = token_data.get("tailnet_name", "your tailnet")

        return jsonify({
            "success": True,
            "sshPublicKey": ssh_public_key,
            "tailnet": tailnet_name,
            "instructions": [
                "1. Copy the SSH public key above",
                "2. On each device you want Aurora to access via SSH, run:",
                f"   echo '{ssh_public_key}' >> ~/.ssh/authorized_keys",
                "3. Ensure the ~/.ssh directory and authorized_keys file have correct permissions:",
                "   chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys",
                "4. Make sure the device is connected to your Tailscale network",
                "5. Verify SSH is enabled on the device (sshd running)",
                "",
                "Once configured, you can ask Aurora to run commands on your devices:",
                "  Example: 'Run uptime on my-server'",
                "  Example: 'Check disk usage on database-1'"
            ],
            "command": f"echo '{ssh_public_key}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        })

    except Exception as e:
        logger.error(f"Error getting SSH setup: {e}", exc_info=True)
        return jsonify({"error": "Failed to get SSH setup"}), 500


@tailscale_bp.route('/tailscale/root-tailnet', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
def tailscale_root_tailnet():
    """Get or set the root tailnet for Tailscale."""
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()

        try:
            if request.method == 'POST':
                data = request.get_json()
                tailnet_id = data.get("tailnetId") or data.get("tailnet_id")

                if not tailnet_id:
                    return jsonify({"error": "tailnetId is required"}), 400

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_preferences (user_id, preference_key, preference_value)
                        VALUES (%s, %s, %s::jsonb)
                        ON CONFLICT (user_id, preference_key)
                        DO UPDATE SET preference_value = EXCLUDED.preference_value, updated_at = NOW()
                    """, (user_id, 'tailscale_root_tailnet', json.dumps(tailnet_id)))
                conn.commit()

                logger.info(f"Set Tailscale root tailnet to {tailnet_id} for user {user_id}")
                return jsonify({
                    "success": True,
                    "tailnetId": tailnet_id,
                    "message": "Root tailnet set successfully"
                })

            # GET - Fetch current root tailnet
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT preference_value FROM user_preferences
                    WHERE user_id = %s AND preference_key = %s
                """, (user_id, 'tailscale_root_tailnet'))
                result = cur.fetchone()

                if result and result[0]:
                    return jsonify({
                        "tailnetId": result[0],
                        "hasRootTailnet": True
                    })

                return jsonify({
                    "tailnetId": None,
                    "hasRootTailnet": False
                })
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Error with Tailscale root tailnet: {e}", exc_info=True)
        return jsonify({"error": "Failed to process root tailnet request"}), 500


@tailscale_bp.route('/tailscale/refresh-token', methods=['POST'])
@limiter.limit("10 per minute")
def tailscale_refresh_token():
    """
    Refresh Tailscale OAuth token.

    This refreshes the access token using stored client credentials.
    """
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        token_data = get_token_data(user_id, "tailscale")
        if not token_data:
            return jsonify({"error": "Tailscale not connected"}), 401

        client_id = token_data.get("client_id")
        client_secret = token_data.get("client_secret")

        if not client_id or not client_secret:
            return jsonify({"error": "Invalid stored credentials"}), 401

        # Refresh the token
        success, new_token_data, error = refresh_oauth_token(client_id, client_secret)

        if not success:
            return jsonify({"error": error}), 401

        # Update stored token data
        token_data["token_data"] = new_token_data
        store_tokens_in_db(user_id, token_data, "tailscale")

        logger.info(f"Tailscale token refreshed for user {user_id}")

        return jsonify({
            "success": True,
            "message": "Token refreshed successfully",
            "expiresAt": new_token_data.get("expires_at")
        })

    except Exception as e:
        logger.error(f"Error refreshing Tailscale token: {e}", exc_info=True)
        return jsonify({"error": "Failed to refresh token"}), 500
