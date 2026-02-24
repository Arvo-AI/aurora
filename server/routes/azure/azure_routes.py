import os, logging
from flask import Blueprint, request, jsonify, Response
import flask
from azure.identity import ClientSecretCredential
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from connectors.azure_connector.auth import azure_login, azure_callback
from connectors.azure_connector.k8s_client import (
    get_sp_object_id, get_aks_clusters, extract_resource_group,
)
from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db  # Using same util for DB storage
import json

azure_bp = Blueprint("azure_bp", __name__)

# ---- Azure Routes ------------------------------------------------------#
@azure_bp.route("/azure/login", methods=["POST", "GET", "OPTIONS"])
def azure_login_route():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    return azure_login()


@azure_bp.route("/azure/setup-script", methods=["GET", "OPTIONS"])
def azure_setup_script():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    try:
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "connectors", "azure_connector", "setup-aurora-access.sh")
        if os.path.exists(script_path):
            with open(script_path, "r", encoding="utf-8") as f:
                script_content = f.read()
            resp = Response(script_content, mimetype="text/plain")
            resp.headers["Content-Disposition"] = "inline; filename=setup-aurora-access.sh"
            resp.headers.update({
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })
            return resp
        return jsonify({"error": "Setup script not found"}), 404
    except Exception as e:
        logging.error("Error serving Azure setup script", exc_info=e)
        return jsonify({"error": "Failed to serve setup script"}), 500


@azure_bp.route("/azure/setup-script-ps1", methods=["GET", "OPTIONS"])
def azure_setup_script_ps1():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    try:
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "connectors", "azure_connector", "setup-aurora-access.ps1")
        if os.path.exists(script_path):
            with open(script_path, "r", encoding="utf-8") as f:
                script_content = f.read()
            resp = Response(script_content, mimetype="text/plain")
            resp.headers["Content-Disposition"] = "inline; filename=setup-aurora-access.ps1"
            resp.headers.update({
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })
            return resp
        return jsonify({"error": "PowerShell setup script not found"}), 404
    except Exception as e:
        logging.error("Error serving Azure PS1 setup script", exc_info=e)
        return jsonify({"error": "Failed to serve PowerShell setup script"}), 500


@azure_bp.route("/azure/callback", methods=["GET", "OPTIONS"])
def azure_callback_route():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    return azure_callback()


@azure_bp.route("/azure/fetch_data", methods=["GET", "POST", "OPTIONS"])
def fetch_data():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 400

        from utils.auth.stateless_auth import get_credentials_from_db
        credentials = get_credentials_from_db(user_id, "azure")
        if credentials:
            try:
                tenant_id = credentials.get("tenant_id")
                client_id = credentials.get("client_id")
                client_secret = credentials.get("client_secret")
                if all([tenant_id, client_id, client_secret]):
                    credential = ClientSecretCredential(
                        tenant_id=str(tenant_id), client_id=str(client_id), client_secret=str(client_secret)
                    )
                    management_token = credential.get_token("https://management.azure.com/.default").token
                    credentials["management_token"] = management_token
                else:
                    credentials = None
            except Exception as token_err:
                logging.error(f"Token generation error: {token_err}")
                credentials = None
        if not credentials:
            return jsonify({"error": "Azure credentials not found. Please re-authenticate."}), 401

        subscription_id = credentials.get("subscription_id")
        subscription_name = credentials.get("subscription_name", "")
        if not subscription_id:
            return jsonify({"error": "No subscription ID found."}), 401

        # Additional processing (billing & k8s data) left as exercise or existing utils
        return jsonify({"status": "success", "subscription_id": subscription_id, "subscription_name": subscription_name})
    except Exception as e:
        logging.error("Error in Azure fetch_data", exc_info=e)
        return jsonify({"error": "Failed to fetch Azure data"}), 500


@azure_bp.route("/azure/clusters", methods=["GET", "OPTIONS"])
def azure_clusters():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 400
        from utils.auth.stateless_auth import get_credentials_from_db
        credentials = get_credentials_from_db(user_id, "azure")
        if not credentials:
            return jsonify({"error": "Azure credentials not found"}), 401
        tenant_id = credentials.get("tenant_id")
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        subscription_id = credentials.get("subscription_id")
        if not all([tenant_id, client_id, client_secret, subscription_id]):
            return jsonify({"error": "Incomplete Azure credentials"}), 400
        credential = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
        management_token = credential.get_token("https://management.azure.com/.default").token
        sp_object_id = get_sp_object_id(tenant_id, client_id, client_secret)
        aks_clusters = get_aks_clusters(management_token, subscription_id, sp_object_id, tenant_id, client_id, client_secret)
        cluster_info = []
        for cluster in aks_clusters:
            name = cluster["name"]
            resource_group = extract_resource_group(cluster["id"])
            if resource_group:
                cluster_info.append({"name": name, "resourceGroup": resource_group, "subscriptionId": subscription_id})
        return jsonify(cluster_info)
    except Exception as e:
        logging.error("Error fetching AKS clusters", exc_info=e)
        return jsonify({"error": "Failed to fetch AKS clusters"}), 500


@azure_bp.route("/api/azure-subscriptions", methods=["GET", "POST", "OPTIONS"])
def azure_subscriptions():
    if request.method == "OPTIONS":
        return create_cors_response()
    try:
        if request.method == "GET":
            user_id = get_user_id_from_request()
            if not user_id:
                return jsonify({"error": "Missing user_id"}), 400
            token_data = get_token_data(user_id, "azure")
            if not token_data:
                logging.warning(f"[AZURE API] No Azure token data found for user {user_id}")
                return jsonify({"error": "No Azure credentials found. Please authenticate with Azure."}), 401
            subscription_id = token_data.get("subscription_id")
            subscription_name = token_data.get("subscription_name", "Azure Subscription")
            if not subscription_id:
                logging.warning(f"[AZURE API] No Azure subscription found for user {user_id}")
                return jsonify({"error": "No Azure subscription found. Please configure your Azure subscription."}), 401
            projects = [{"projectId": subscription_id, "name": subscription_name, "enabled": True}]
            return jsonify({"projects": projects}), 200
        else:
            data = request.get_json()
            projects = data.get("projects", [])
            logging.info(f"Azure subscription selection update received: {projects}")
            return jsonify({"status": "success"})
    except Exception as e:
        logging.error("Error in azure_subscriptions", exc_info=e)
        return jsonify({"error": "Failed to process Azure subscriptions"}), 500
