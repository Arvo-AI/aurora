"""Account management routes for connected accounts."""
import logging
from flask import Blueprint, request, jsonify, session
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.db.db_utils import connect_to_db_as_admin, connect_to_db_as_user
from utils.auth.token_management import get_token_data
from utils.secrets.secret_ref_utils import delete_user_secret, SUPPORTED_SECRET_PROVIDERS
import requests
import os

account_management_bp = Blueprint("account_management", __name__)


@account_management_bp.route("/api/connected-accounts/<user_id>", methods=["GET", "OPTIONS"])
def get_connected_accounts(user_id):
    """Get connected account information for a user."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    try:
        # Get authenticated user identity from X-User-ID header
        authenticated_user_id = get_user_id_from_request()
        
        if not authenticated_user_id:
            logging.warning("No authenticated user found for connected accounts request")
            return jsonify({"error": "Unauthorized"}), 401
        
        if authenticated_user_id != user_id:
            logging.warning(f"SECURITY: User {authenticated_user_id} attempted to access connected accounts for {user_id}")
            return jsonify({"error": "Unauthorized access to user data"}), 403
        
        # Connect to the database
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        
        # ------------------------------
        # 1) OAuth / secret-based providers (user_tokens)
        # ------------------------------
        cursor.execute(
            """
            SELECT provider, subscription_id, subscription_name, timestamp
            FROM user_tokens 
            WHERE user_id = %s AND secret_ref IS NOT NULL AND is_active = TRUE
            """,
            (user_id,),
        )

        rows = cursor.fetchall()

        accounts: dict = {}

        for provider, subscription_id, subscription_name, timestamp in rows:
            
            # Get token data from Vault
            token_data = get_token_data(user_id, provider)
            if not token_data:
                continue
            
            # Extract account information based on provider
            account_info = {"isConnected": True}
            
            # Only fetch full credentials when we actually need them for display
            if provider == "gcp":
                # For GCP, we need email from credentials for display
                token_data = get_token_data(user_id, provider)
                if not token_data:
                    continue
                account_info["email"] = token_data.get("email", "Unknown")
                account_info["name"] = token_data.get("name", "Google Cloud")
                account_info["displayText"] = account_info["email"]
            elif provider == "aws":
                # For AWS, try to get account ID from credentials
                token_data = get_token_data(user_id, provider)
                if not token_data:
                    continue
                account_info["accountId"] = token_data.get("aws_account_id", "Unknown")
                account_info["name"] = f"AWS Account"
                account_info["displayText"] = f"Account {account_info['accountId']}"
            elif provider == "azure":
                # For Azure, use subscription information from DB (no need to fetch credentials)
                account_info["subscriptionId"] = subscription_id or "Unknown"
                account_info["subscriptionName"] = subscription_name or "Azure Subscription"
                account_info["name"] = subscription_name or "Azure"
                account_info["displayText"] = subscription_name or "Azure Subscription"
            else:
                # For other providers, use basic info from DB without fetching credentials
                account_info["name"] = provider.capitalize()
                account_info["displayText"] = subscription_name or subscription_id or provider.capitalize()
            
            accounts[provider] = account_info
        
        # ------------------------------
        # 2) Role-based connections (user_connections – AWS today)
        # ------------------------------
        cursor.execute(
            """
            SELECT provider, account_id, role_arn, last_verified_at
            FROM user_connections
            WHERE user_id = %s AND status = 'active'
            """,
            (user_id,),
        )

        for provider, account_id, role_arn, last_verified in cursor.fetchall():
            if provider in accounts:  # already filled (unlikely)
                continue

            if provider == "aws":
                accounts[provider] = {
                    "isConnected": True,
                    "accountId": account_id,
                    "roleArn": role_arn,
                    "name": "AWS Account",
                    "displayText": f"Account {account_id}",
                }

        cursor.close()
        conn.close()
        
        return jsonify({"accounts": accounts})
    
    except Exception as e:
        logging.error(f"Error getting connected accounts: {e}", exc_info=True)
        return jsonify({"error": "Failed to get connected accounts"}), 500


@account_management_bp.route("/api/connected-accounts/<user_id>/<provider>", methods=["DELETE", "OPTIONS"])
def delete_connected_account(user_id, provider):
    """Delete stored credentials for *provider* so tools can no longer use them."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        # Get authenticated user identity from X-User-ID header
        authenticated_user_id = get_user_id_from_request()
        
        if not authenticated_user_id:
            logging.warning("No authenticated user found for delete connected account request")
            return jsonify({"error": "Unauthorized"}), 401
        
        if authenticated_user_id != user_id:
            logging.warning(f"SECURITY: User {authenticated_user_id} attempted to delete connected account for {user_id}")
            return jsonify({"error": "Unauthorized access to user data"}), 403
        
        # Get secret_ref BEFORE deleting to clear cache properly
        secret_ref = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, provider)
            )
            result = cursor.fetchone()
            if result:
                secret_ref = result[0]
            cursor.close()
            conn.close()
        except Exception as e:
            logging.warning(f"Failed to get secret_ref before deletion: {e}")
        
        provider_lc = provider.lower()
        deletion_ok = True
        deleted = 0

        if provider_lc in SUPPORTED_SECRET_PROVIDERS:
            # For providers that use Vault (GCP/Azure etc.)
            deletion_ok, deleted = delete_user_secret(user_id, provider_lc)
        else:
            # For providers that don't use Vault, delete from DB directly
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, provider)
            )
            deleted = cursor.rowcount
            conn.commit()

            cursor.close()
            conn.close()

        # --------------------------------------------------
        # Clear caching layers after token deletion
        # --------------------------------------------------
        # 1) Clear GCP auth caches if provider is GCP
        if provider_lc == "gcp":
            try:
                from chat.backend.agent.tools.auth.gcp_cached_auth import clear_gcp_cache_for_user
                clear_gcp_cache_for_user(user_id)
                logging.info(f"Cleared GCP caches for user {user_id}")
            except Exception as e:
                logging.warning(f"Failed to clear GCP caches for user {user_id}: {e}")
            
            # Clear GCP root project preference
            try:
                conn = connect_to_db_as_admin()
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM user_preferences WHERE user_id = %s AND preference_key = 'gcp_root_project'",
                    (user_id,)
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logging.info(f"Cleared GCP root project preference for user {user_id}")
                cursor.close()
                conn.close()
            except Exception as e:
                logging.warning(f"Failed to clear GCP root project preference for user {user_id}: {e}")
        
        # 2) Clear Redis secret cache for this secret_ref (if present)
        if secret_ref:
            try:
                from utils.secrets.secret_cache import clear_secret_cache
                clear_secret_cache(secret_ref)
                logging.info(f"Cleared secret cache for {provider}: {secret_ref}")
            except Exception as e:
                logging.warning(f"Failed to clear secret cache: {e}")

        # --------------------------------------------------------------
        # AWS now lives in user_connections → perform per-account cleanup
        # --------------------------------------------------------------
        if provider_lc == "aws":
            from utils.db.connection_utils import (
                list_active_connections,
                delete_connection_secret,
            )
            
            active = list_active_connections(user_id)
            if not active:
                logging.info("No active AWS connections found for user %s", user_id)
            
            for conn in active:
                acc_id = conn["account_id"]
                _ok = delete_connection_secret(user_id, "aws", acc_id)
                try:
                    from utils.auth.stateless_auth import invalidate_cached_aws_creds
                    invalidate_cached_aws_creds(user_id, acc_id)
                except Exception:
                    pass
                deletion_ok = deletion_ok and _ok
            
            return jsonify({"success": True, "message": "AWS connection(s) removed"}), 200
        
        # Handle other providers (Aurora and anything not handled above)
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, provider)
            )

            deleted = cursor.rowcount
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as db_err:
            logging.error(f"Database error during {provider} disconnect: {db_err}")
            raise

        # Remove from Vault
        if provider_lc in SUPPORTED_SECRET_PROVIDERS:
            deletion_ok, _ = delete_user_secret(user_id, provider_lc)

        
        # Idempotent behaviour: If there were no credentials stored in the first place
        # treat the request as successfully processed. This prevents unnecessary 404
        # errors that bubble up to the frontend when a user disconnects a provider
        # that was never connected (common after manual DB cleanup).
        if deleted == 0 and deletion_ok:
            return jsonify({"success": True, "message": "No tokens found for provider – nothing to delete"}), 200
        
        if not deletion_ok:
            # Secret deletion failed but DB cleaned up – warn client
            return jsonify({"success": True, "message": f"Removed local reference for {provider}. Failed to delete cloud secret."}), 206

        return jsonify({"success": True, "message": f"Removed {provider} credentials"}), 200

    except Exception as e:
        logging.error(f"Error deleting connected account for {user_id}/{provider}: {e}", exc_info=True)
        return jsonify({"error": "Failed to delete connected account"}), 500


@account_management_bp.route("/api/getUserId", methods=["GET", "OPTIONS"])
def get_user_id():
    """Get the current user ID from session or request."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    try:
        # Get user_id from multiple sources: stateless auth (GCP) or session (AWS) or query parameter
        user_id = get_user_id_from_request()
        
        # If no user_id found, return error instead of generating fallback
        if not user_id:
            logging.warning("No user ID found in request - user should authenticate via Auth.js")
            return jsonify({"error": "No user ID found. Please authenticate via Auth.js."}), 401
        
        return jsonify({"userId": user_id}), 200
        
    except Exception as e:
        logging.error(f"Error getting user ID: {e}", exc_info=True)
        return jsonify({"error": "Failed to get user ID"}), 500


@account_management_bp.route("/user_tokens", methods=["GET", "OPTIONS"])
def get_user_tokens():
    """Fetch user tokens from user_tokens table."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    conn = None
    cursor = None
    try:
        # Debug-level request details
        logging.debug(f"get_user_tokens called - method: {request.method}")
        
        # SECURITY FIX: Validate user authentication properly
        user_id_from_request = get_user_id_from_request()
        user_id_from_args = request.args.get("user_id")
        
        logging.debug(
            "get_user_tokens - user_id from request: %s, from args: %s",
            user_id_from_request,
            user_id_from_args,
        )
        
        # SECURITY: Unified authentication using get_user_id_from_request()
        authenticated_user_id = user_id_from_request

        if authenticated_user_id:
            logging.debug("Authenticated user: %s", authenticated_user_id)
            # SECURITY: Clear any old Flask sessions when authenticated user is present
            if session:
                session.clear()
                logging.debug("Cleared Flask session for authenticated user %s", authenticated_user_id)
            
            # SECURITY: Validate that requested user_id matches authenticated user
            if user_id_from_args and user_id_from_args != authenticated_user_id:
                logging.warning(f"SECURITY: User {authenticated_user_id} attempted to access data for {user_id_from_args}")
                return jsonify({"error": "Unauthorized access to user data"}), 403
        
        # Final validation: we must have an authenticated user
        if not authenticated_user_id:
            logging.warning("No authenticated user found, returning empty array")
            return jsonify([]), 200
        
        user_id = authenticated_user_id
        logging.debug("Final authenticated user_id: %s", user_id)
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        cursor.execute(
            """
            SELECT subscription_id, subscription_name, tenant_id, client_id, provider, email
            FROM user_tokens 
            WHERE user_id = %s AND is_active = TRUE AND secret_ref IS NOT NULL
            """,
            (user_id,)
        )
        tokens = cursor.fetchall()

        logging.debug("Found %d tokens for user %s", len(tokens), user_id)
        
        if not tokens:
            return jsonify([]), 200
            
        # Format the results (exclude any credential values)
        formatted_tokens = [{
            'subscription_id': token[0],
            'subscription_name': token[1],
            'tenant_id': token[2],
            'client_id': token[3],
            'provider': token[4],
            'email': token[5],
        } for token in tokens]

        logging.debug(
            "Returning %d formatted tokens (metadata only)", len(formatted_tokens)
        )
        return jsonify(formatted_tokens), 200

    except Exception as e:
        logging.error(f"Error fetching user tokens: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch user tokens"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
