"""Account management routes for connected accounts."""
import logging
from flask import Blueprint, request, jsonify
from utils.web.cors_utils import create_cors_response
from utils.auth.rbac_decorators import require_auth_only
from utils.db.db_utils import connect_to_db_as_admin, connect_to_db_as_user
from utils.auth.token_management import get_token_data
from utils.auth.stateless_auth import get_org_id_from_request
from utils.secrets.secret_ref_utils import delete_user_secret, SUPPORTED_SECRET_PROVIDERS
import requests
import os

account_management_bp = Blueprint("account_management", __name__)


def _validate_provider_connection(provider: str, token_data: dict) -> bool:
    """Return True only if the stored credentials actually work.

    Delegates to the unified PROVIDER_CHECKERS in connector_status so that
    both /api/connected-accounts and /api/connectors/status agree.
    """
    from routes.connector_status import PROVIDER_CHECKERS

    checker = PROVIDER_CHECKERS.get(provider)
    if checker is None:
        return True
    try:
        result = checker(token_data)
        return result.get("connected", False)
    except Exception:
        return False


@account_management_bp.route("/api/connected-accounts/<target_user_id>", methods=["OPTIONS"])
def get_connected_accounts_options(target_user_id):
    return create_cors_response()


@account_management_bp.route("/api/connected-accounts/<target_user_id>", methods=["GET"])
@require_auth_only
def get_connected_accounts(user_id, target_user_id):
    """Get connected account information for a user."""
    conn = None
    cursor = None
    try:
        if user_id != target_user_id:
            logging.warning(f"SECURITY: User {user_id} attempted to access connected accounts for {target_user_id}")
            return jsonify({"error": "Not found"}), 404
        
        org_id = get_org_id_from_request()

        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        
        # ------------------------------
        # 1) OAuth / secret-based providers (user_tokens)
        # ------------------------------
        cursor.execute(
            """
            SELECT DISTINCT ON (provider)
                   provider, subscription_id, subscription_name, timestamp, user_id
            FROM user_tokens 
            WHERE (user_id = %s OR org_id = %s)
              AND secret_ref IS NOT NULL
              AND is_active = TRUE
            ORDER BY provider, CASE WHEN user_id = %s THEN 0 ELSE 1 END
            """,
            (user_id, org_id, user_id),
        )

        rows = cursor.fetchall()

        accounts: dict = {}

        # Validate all providers in parallel to avoid serial latency
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _resolve_row(row):
            provider, subscription_id, subscription_name, timestamp, token_owner_id = row
            token_data = get_token_data(token_owner_id, provider)
            if not token_data:
                return None
            token_data = dict(token_data)
            token_data["_user_id"] = token_owner_id
            if not _validate_provider_connection(provider, token_data):
                return None
            account_info = {"isConnected": True}
            if provider == "gcp":
                account_info["email"] = token_data.get("email", "Unknown")
                account_info["name"] = token_data.get("name", "Google Cloud")
                account_info["displayText"] = account_info["email"]
            elif provider == "aws":
                account_info["accountId"] = token_data.get("aws_account_id", "Unknown")
                account_info["name"] = f"AWS Account"
                account_info["displayText"] = f"Account {account_info['accountId']}"
            elif provider == "azure":
                account_info["subscriptionId"] = subscription_id or "Unknown"
                account_info["subscriptionName"] = subscription_name or "Azure Subscription"
                account_info["name"] = subscription_name or "Azure"
                account_info["displayText"] = subscription_name or "Azure Subscription"
            else:
                account_info["name"] = provider.capitalize()
                account_info["displayText"] = subscription_name or subscription_id or provider.capitalize()
            return (provider, account_info)

        executor = ThreadPoolExecutor(max_workers=8)
        try:
            futures = {executor.submit(_resolve_row, row): row[0] for row in rows}
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=12)
                    if result:
                        provider, account_info = result
                        accounts[provider] = account_info
                except Exception as exc:
                    logging.warning("connected-accounts check for %s raised: %s", futures[future], exc)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        
        # ------------------------------
        # 2) Role-based connections (user_connections – AWS today)
        # ------------------------------
        cursor.execute(
            """
            SELECT provider, account_id, role_arn, last_verified_at
            FROM user_connections
            WHERE (user_id = %s OR org_id = %s) AND status = 'active'
            ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END
            """,
            (user_id, org_id, user_id),
        )

        for provider, account_id, role_arn, last_verified in cursor.fetchall():
            if provider in accounts:
                continue

            account_info = {
                "isConnected": True,
                "name": f"{provider.upper()} Account" if provider == "aws" else provider.capitalize(),
                "displayText": f"Account {account_id}" if account_id else provider.capitalize(),
            }

            if provider == "aws":
                account_info["accountId"] = account_id
                account_info["roleArn"] = role_arn
                account_info["name"] = "AWS Account"
            elif provider == "gcp":
                account_info["projectId"] = account_id
                account_info["name"] = "Google Cloud"
                account_info["displayText"] = account_id or "Google Cloud"
            elif provider == "azure":
                account_info["subscriptionId"] = account_id
                account_info["name"] = "Azure"
                account_info["displayText"] = account_id or "Azure Subscription"

            accounts[provider] = account_info

        cursor.close()
        conn.close()
        
        return jsonify({"accounts": accounts})

    except Exception as e:
        logging.error(f"Error getting connected accounts: {e}", exc_info=True)
        return jsonify({"error": "Failed to get connected accounts"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@account_management_bp.route("/api/connected-accounts/<target_user_id>/<provider>", methods=["OPTIONS"])
def delete_connected_account_options(target_user_id, provider):
    return create_cors_response()


@account_management_bp.route("/api/connected-accounts/<target_user_id>/<provider>", methods=["DELETE"])
@require_auth_only
def delete_connected_account(user_id, target_user_id, provider):
    """Delete stored credentials for *provider* so tools can no longer use them."""
    try:
        if user_id != target_user_id:
            logging.warning(f"SECURITY: User {user_id} attempted to delete connected account for {target_user_id}")
            return jsonify({"error": "Not found"}), 404
        
        org_id = get_org_id_from_request()

        # Get secret_ref BEFORE deleting to clear cache properly
        secret_ref = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = %s",
                (user_id, org_id, provider)
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
                "DELETE FROM user_tokens WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND provider = %s",
                (user_id, org_id, provider)
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
                    "DELETE FROM user_preferences WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND preference_key = 'gcp_root_project'",
                    (user_id, org_id)
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
        
        # Idempotent behaviour: If there were no credentials stored in the first place
        # treat the request as successfully processed. This prevents unnecessary 404
        # errors that bubble up to the frontend when a user disconnects a provider
        # that was never connected (common after manual DB cleanup).
        if deleted == 0 and deletion_ok:
            return jsonify({"success": True, "message": "No tokens found for provider – nothing to delete"}), 200
        
        if not deletion_ok:
            return jsonify({"success": True, "message": f"Removed local reference for {provider}. Failed to delete cloud secret."}), 206

        return jsonify({"success": True, "message": f"Removed {provider} credentials"}), 200

    except Exception as e:
        logging.error(f"Error deleting connected account for {user_id}/{provider}: {e}", exc_info=True)
        return jsonify({"error": "Failed to delete connected account"}), 500


@account_management_bp.route("/api/getUserId", methods=["OPTIONS"])
def get_user_id_options():
    return create_cors_response()


@account_management_bp.route("/api/getUserId", methods=["GET"])
@require_auth_only
def get_user_id(user_id):
    """Get the current user ID from session or request."""
    try:
        return jsonify({"userId": user_id}), 200
        
    except Exception as e:
        logging.error(f"Error getting user ID: {e}", exc_info=True)
        return jsonify({"error": "Failed to get user ID"}), 500


@account_management_bp.route("/user_tokens", methods=["OPTIONS"])
def get_user_tokens_options():
    return create_cors_response()


@account_management_bp.route("/user_tokens", methods=["GET"])
@require_auth_only
def get_user_tokens(user_id):
    """Fetch user tokens from user_tokens table."""
    conn = None
    cursor = None
    try:
        logging.debug(f"get_user_tokens called - method: {request.method}")
        
        user_id_from_args = request.args.get("user_id")
        
        logging.debug(
            "get_user_tokens - user_id from auth: %s, from args: %s",
            user_id,
            user_id_from_args,
        )

        if user_id_from_args and user_id_from_args != user_id:
            logging.warning(f"SECURITY: User {user_id} attempted to access data for {user_id_from_args}")
            return jsonify({"error": "Unauthorized access to user data"}), 403
        
        org_id = get_org_id_from_request()
        logging.debug("Final authenticated user_id: %s", user_id)
        
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        if org_id:
            cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
        conn.commit()
        cursor.execute(
            """
            SELECT DISTINCT ON (provider)
                   subscription_id, subscription_name, tenant_id, client_id, provider, email
            FROM user_tokens 
            WHERE (user_id = %s OR org_id = %s)
              AND is_active = TRUE
              AND secret_ref IS NOT NULL
            ORDER BY provider, CASE WHEN user_id = %s THEN 0 ELSE 1 END
            """,
            (user_id, org_id, user_id)
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
