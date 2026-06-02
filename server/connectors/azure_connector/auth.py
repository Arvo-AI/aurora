from flask import request, session, jsonify
import os, logging
from dotenv import load_dotenv
from connectors.azure_connector.billing import fetch_subscriptions
from utils.auth.token_management import store_tokens_in_db
from azure.identity import ClientSecretCredential

load_dotenv()

def azure_login(data=None):
    """Handle Azure login with service principal credentials."""
    try:
        # Get data from parameter or request
        if data is None:
            data = request.get_json()
            
        user_id = data.get("userId")
        # Service principal flow
        # Map the frontend parameter names to what we expect
        tenant_id = data.get("tenantId") or data.get("tenant")  # Support both formats
        client_id = data.get("clientId") or data.get("appId")   # Support both formats
        client_secret = data.get("clientSecret") or data.get("password")  # Support both formats
        
        # Get subscription information if provided
        provided_subscription_id = data.get("subscriptionId") or data.get("subscription_id", "")
        provided_subscription_name = data.get("subscriptionName") or data.get("subscription_name", "")

        if not all([user_id, tenant_id, client_id, client_secret]):
            return jsonify({"error": "Missing required credentials"}), 400

        # Create a ClientSecretCredential object
        credential = ClientSecretCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret
        )

        try:
            # Get access token
            token = credential.get_token("https://management.azure.com/.default")
            if not token:
                return jsonify({"error": "Failed to get Azure token"}), 401

            management_token = token.token

            # Store user_id in session for compatibility, but credentials come from database
            session["user_id"] = user_id

            # Get subscriptions to verify access
            subscriptions = fetch_subscriptions(management_token)
            if not subscriptions:
                return jsonify({"error": "No enabled subscription found"}), 400

            # Find first enabled subscription
            subscription = None
            for sub in subscriptions:
                if sub.get("state") == "Enabled":
                    subscription = sub
                    break

            if not subscription:
                return jsonify({"error": "No enabled subscription found"}), 400

            # Subscription information is stored in database, not session
            logging.info(
                f"Selected Azure subscription: "
                f"{subscription['displayName']} "
                f"({subscription['subscriptionId']})"
            )

            # If frontend provided subscription info, use it for storage
            stored_subscription_id = provided_subscription_id or subscription["subscriptionId"]
            stored_subscription_name = provided_subscription_name or subscription["displayName"]

            # Parse optional read-only credentials for Ask mode (if provided)
            read_only_payload = data.get("readOnlyCredentials") or data.get("read_only_credentials")
            read_only_block = None
            if read_only_payload:
                if not isinstance(read_only_payload, dict):
                    return jsonify({"error": "readOnlyCredentials must be an object"}), 400

                ro_client_id = read_only_payload.get("clientId") or read_only_payload.get("appId")
                ro_client_secret = read_only_payload.get("clientSecret") or read_only_payload.get("password")
                ro_tenant_id = read_only_payload.get("tenantId") or read_only_payload.get("tenant")
                ro_subscription_id = read_only_payload.get("subscriptionId") or read_only_payload.get("subscription_id")

                if not ro_client_id or not ro_client_secret:
                    return jsonify({"error": "readOnlyCredentials must include clientId and clientSecret"}), 400

                read_only_block = {
                    "tenant_id": ro_tenant_id or tenant_id,
                    "client_id": ro_client_id,
                    "client_secret": ro_client_secret,
                    "subscription_id": ro_subscription_id or stored_subscription_id,
                }

            # Store tokens in database with expiry
            from time import time
            token_data = {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "client_secret": client_secret,
                "access_token": management_token,
                "management_token": management_token,
                "expires_at": token.expires_on,
                "subscription_id": stored_subscription_id,
                "subscription_name": stored_subscription_name,
            }

            if read_only_block:
                token_data["read_only"] = read_only_block
            
            # Store in user_tokens table with subscription information
            store_tokens_in_db(
                user_id, 
                token_data, 
                "azure", 
                subscription_name=stored_subscription_name,
                subscription_id=stored_subscription_id
            )

            # Credentials are stored in database as single source of truth
            # Session storage removed to prevent stale credential issues

            return jsonify({
                "message": "Successfully logged in to Azure",
                "subscription_id": subscription["subscriptionId"],
                "subscription_name": subscription["displayName"]
            })

        except Exception as e:
            logging.error(f"Error validating Azure credentials: {e}", exc_info=True)
            return jsonify({"error": "Invalid Azure credentials"}), 401

    except Exception as e:
        logging.error(f"Error in Azure login: {e}", exc_info=True)
        return jsonify({"error": "Failed to process Azure login"}), 500


