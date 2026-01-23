from flask import Flask, request, session, jsonify, redirect
from msal import ConfidentialClientApplication
import os, logging, urllib.parse, jwt
from dotenv import load_dotenv
from connectors.azure_connector.billing import fetch_subscriptions
from connectors.azure_connector.k8s_client import get_aks_clusters
from utils.auth.token_management import store_tokens_in_db, get_token_data
from azure.identity import ClientSecretCredential
from utils.db.db_utils import connect_to_db_as_admin
from utils.logging.secure_logging import mask_credential_value

load_dotenv()

# ---------------- AURORA APP CONFIGURATION ----------------
# These credentials are for Aurora's OAuth app registration, not user credentials
# They're needed for OAuth redirect flows and on-behalf-of token exchanges

AZURE_AUTHORITY = f"https://login.microsoftonline.com/common"
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")

AZURE_SCOPES = [f"api://{AZURE_CLIENT_ID}/obo_flow"]

# Initialize MSAL app for OAuth flows
msal_app = ConfidentialClientApplication(
    AZURE_CLIENT_ID,
    authority=AZURE_AUTHORITY,
    client_credential=AZURE_CLIENT_SECRET
)

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
            logging.error(f"Error validating Azure credentials: {str(e)}")
            return jsonify({"error": "Invalid Azure credentials"}), 401

    except Exception as e:
        logging.error(f"Error in Azure login: {e}")
        return jsonify({"error": str(e)}), 500



def azure_callback():
    """Handles the callback from Microsoft Entra ID authentication."""
    FRONTEND_URL = os.getenv("FRONTEND_URL")

    try:
        # Retrieve query parameters (fixing the 415 Unsupported Media Type error)
        state = request.args.get("state")
        code = request.args.get("code")  # Extract authorization code from the URL

        if not state:
            raise ValueError("State parameter missing.")
        if not code:
            raise ValueError("Authorization code is missing.")

        user_id = urllib.parse.unquote(state)  # Decode user_id from state

        # Retrieve stored authentication flow session
        azure_flow = session.get("azure_auth_flow")
        if not azure_flow:
            raise ValueError("Azure auth flow not found in session. Session may have expired.")

        # Exchange authorization code for access token
        azure_result = msal_app.acquire_token_by_auth_code_flow(azure_flow, request.args)
        if "error" in azure_result:
            raise ValueError(f"Token acquisition failed: {azure_result['error_description']}")

        # Store the access token in session
        azure_access_token = azure_result.get("access_token")
        logging.debug(f"Access token: {azure_access_token}");
        if not azure_access_token:
            raise ValueError("Azure access token is missing in response.")

        session["access_token"] = azure_access_token

        # Extract user_id from state parameter (strip the "azure_" prefix)
        #user_id = state[len("azure_"):] if state.startswith("azure_") else None
        
        # (Optionally) decode the id_token to log the tenant ID:
        id_token = azure_result.get("id_token")
        if not id_token:
            raise ValueError("Azure ID token is missing in response.")

        decoded_token = jwt.decode(id_token, options={"verify_signature": False})  # Decode without verifying signature
        tenant_id = decoded_token.get("tid")  # Tenant ID
        # SECURITY: Mask tenant ID in logs
        masked_tenant = mask_credential_value(tenant_id, 8) if tenant_id else "unknown"
        logging.info(f"User authenticated from Tenant: {masked_tenant}, User ID: {user_id}")

        # Remove auth session
        session.pop("azure_auth_flow", None)

        return redirect(f"{FRONTEND_URL}?login=azure_success")

    except Exception as e:
        logging.error(f"Entra ID Callback error: {e}")
        return redirect(f"{FRONTEND_URL}?login=azure_failed")
