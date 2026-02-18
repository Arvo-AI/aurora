"""GCP billing routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_refresh import refresh_token_if_needed
from connectors.gcp_connector.auth.oauth import get_credentials
from utils.auth.token_management import get_token_data
from connectors.gcp_connector.gcp.projects import get_project_list
from connectors.gcp_connector.billing import store_bigquery_data, is_bigquery_enabled

gcp_billing_bp = Blueprint("gcp_billing", __name__)

@gcp_billing_bp.route("/billing", methods=["POST"])
def billing():
    try:
        logging.info("running the billing api")
        data = request.get_json()
        user_id = data.get("userId")
        provider = data.get("X-Provider", "gcp")  # Default to GCP if not specified

        if not user_id:
            return jsonify({"error": "Missing user_id in request body"}), 400

        # Refresh token if needed before proceeding
        try:
            refresh_token_if_needed(user_id, provider)
        except Exception as e:
            return jsonify({"error": "Token refresh failed"}), 401

        logging.info(f"Received user id:'{user_id}' successfully.")
        token_data = get_token_data(user_id, provider)
        if not token_data:
            logging.warning(f"No token data found for user_id: {user_id}, provider: {provider}")
            return jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401
        credentials = get_credentials(token_data)
        logging.info(f"Credentials successfully retrieved for user_id:'{user_id}'")

        # Rest of the existing function code...
        projects = get_project_list(credentials)
        if not projects:
            return jsonify({"message": "No projects found for the authenticated user."}), 404

        processed_projects = []
        for project in projects:
            project_id = project.get('projectId')
            if not project_id:
                continue

            try:
                if is_bigquery_enabled(project_id, credentials):
                    logging.info(f"Storing billing data for project: {project_id}")
                    store_bigquery_data(credentials, project_id, user_id)
                    processed_projects.append(project_id)
                else:
                    logging.info(f"BigQuery not enabled for project: {project_id}, skipping")
            except ValueError as e:
                if "Permission Denied" in str(e):
                    logging.warning(f"Permission denied for project {project_id}, likely deleted or inaccessible. Skipping.")
                    continue
                else:
                    # Re-raise other ValueError exceptions
                    raise e
            except Exception as e:
                logging.warning(f"Error checking project {project_id}: {e}. Skipping.")
                continue

        if processed_projects:
            return jsonify({"message": "Billing successful"}), 200
        else:
            return jsonify({"message": "No projects with BigQuery API enabled were processed."}), 404

    except Exception as e:
        logging.error(f"Error in billing: {e}", exc_info=True)
        return jsonify({"billing_error": "Failed to process billing"}), 500
