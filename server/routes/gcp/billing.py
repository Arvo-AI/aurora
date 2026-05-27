"""GCP billing routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_refresh import refresh_token_if_needed
from connectors.gcp_connector.auth.oauth import get_credentials
from utils.auth.token_management import get_token_data
from connectors.gcp_connector.gcp.projects import get_project_list
from connectors.gcp_connector.billing import store_bigquery_data, is_bigquery_enabled
from connectors.gcp_connector.auth.multi_sa import load_gcp_connections_with_creds

gcp_billing_bp = Blueprint("gcp_billing", __name__)

@gcp_billing_bp.route("/billing", methods=["POST"])
@require_permission("connectors", "read")
def billing(user_id):
    try:
        logging.info("running the billing api")
        data = request.get_json()
        provider = data.get("X-Provider", "gcp") if data else "gcp"

        # Multi-SA path: build a list of (label, credentials) to iterate.
        # Falls back to the legacy single-token path when no user_connections
        # rows exist (OAuth users, pre-migration).
        sa_pairs = load_gcp_connections_with_creds(user_id)
        creds_list = [(conn.get("account_id", "sa"), creds) for conn, creds in sa_pairs]

        if not creds_list:
            try:
                refresh_token_if_needed(user_id, provider)
            except Exception as e:
                logging.error(f"Token refresh failed: {e}", exc_info=True)
                return jsonify({"error": "Token refresh failed"}), 401

            token_data = get_token_data(user_id, provider)
            if not token_data:
                logging.warning(f"No token data found for user_id: {user_id}, provider: {provider}")
                return jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401
            creds_list = [("legacy", get_credentials(token_data))]

        processed_projects = []
        for sa_label, credentials in creds_list:
            try:
                projects = get_project_list(credentials) or []
            except Exception as e:
                logging.warning("Failed to list projects for sa=%s: %s", sa_label[:12], type(e).__name__)
                continue
            for project in projects:
                project_id = project.get('projectId')
                if not project_id or project_id in processed_projects:
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
                        logging.warning(f"Permission denied for project {project_id}, skipping.")
                        continue
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
