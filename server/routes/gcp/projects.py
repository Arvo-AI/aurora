"""GCP project management routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_preference
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_refresh import refresh_token_if_needed
from connectors.gcp_connector.auth.oauth import get_credentials
from utils.auth.token_management import get_token_data
from connectors.gcp_connector.gcp.projects import get_project_list
from connectors.gcp_connector.auth.service_accounts import (
    get_aurora_service_account_email,
    update_service_account_project_access,
    get_gcp_auth_type,
    GCP_AUTH_TYPE_SA,
)
from connectors.gcp_connector.billing import has_active_billing
from googleapiclient.discovery import build

gcp_projects_bp = Blueprint("gcp_projects", __name__)

@gcp_projects_bp.route("/api/gcp/projects", methods=["POST"])
@require_permission("connectors", "read")
def get_projects(user_id):
    """Get all GCP projects with billing status for the authenticated user."""
    try:
        logging.info("Fetching GCP projects with billing status")
        provider = "gcp"

        # Refresh token if needed before proceeding
        try:
            refresh_token_if_needed(user_id, provider)
        except Exception as e:
            logging.error(f"Token refresh failed: {e}", exc_info=True)
            return jsonify({"error": "Token refresh failed"}), 401

        logging.info(f"Received user id:'{user_id}' successfully.")
        token_data = get_token_data(user_id, provider)
        if not token_data:
            logging.warning(f"No token data found for user_id: {user_id}, provider: {provider}")
            return jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401
        credentials = get_credentials(token_data)
        logging.info(f"Credentials successfully retrieved for user_id:'{user_id}'")

        projects = get_project_list(credentials)
        logging.info(f"Returning {len(projects)} accessible projects")
        if not projects:
            return jsonify({"message": "No projects found for the authenticated user.", "projects": []}), 200

        logging.info(f"Found {len(projects)} GCP projects. Checking billing status...")

        # Process each project to include billing status
        project_list = []
        for project in projects:
            project_id = project.get('projectId')
            project_name = project.get('name', project_id)
            project_number = project.get('projectNumber')
            lifecycle_state = project.get('lifecycleState', 'UNKNOWN')
            
            if not project_id:
                continue

            # Check billing status for this project
            billing_active = has_active_billing(project_id, credentials)
            
            project_info = {
                "projectId": project_id,
                "name": project_name,
                "projectNumber": project_number,
                "lifecycleState": lifecycle_state,
                "billingEnabled": billing_active,
                "available": billing_active  # Projects are only available if billing is enabled
            }
            
            project_list.append(project_info)
            logging.info(f"Project {project_id}: billing_enabled={billing_active}")

        # Sort projects: billing-enabled projects first, then by name
        project_list.sort(key=lambda x: (not x["billingEnabled"], x["name"]))

        # Get current root project preference
        root_project = get_user_preference(user_id, 'gcp_root_project')

        # Mark which project is the root project
        for project in project_list:
            project['isRootProject'] = project['projectId'] == root_project

        return jsonify({
            "projects": project_list,
            "count": len(project_list),
            "billing_enabled_count": len([p for p in project_list if p["billingEnabled"]]),
            "root_project": root_project
        }), 200

    except Exception as e:
        logging.error(f"Error in get_projects: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch GCP projects"}), 500


def _load_gcp_token(user_id):
    """Fetch GCP token data; return (token_data, None) or (None, error_response)."""
    token_data = get_token_data(user_id, "gcp")
    if not token_data:
        logging.warning(f"No token data found for user_id: {user_id}, provider: gcp")
        return None, (jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401)
    return token_data, None


@gcp_projects_bp.route("/api/gcp/sa-project-access", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def sa_project_access_get(user_id):
    """List projects with SA access flag."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        token_data, err = _load_gcp_token(user_id)
        if err:
            return err

        # Service-account mode: surface auto-discovered accessible_projects
        # list with all entries marked enabled. Aurora doesn't manage IAM
        # bindings in SA mode — the uploaded SA already has whatever roles
        # the user granted it directly in GCP.
        if get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_SA:
            accessible = token_data.get("accessible_projects") or []
            root_project = get_user_preference(user_id, 'gcp_root_project')
            result = []
            for proj in accessible:
                pid = proj.get("project_id")
                if not pid:
                    continue
                result.append({
                    "projectId": pid,
                    "name": proj.get("name") or pid,
                    "enabled": True,
                    "hasPermission": True,
                    "isRootProject": pid == root_project,
                })
            result.sort(key=lambda x: x['name'])
            return jsonify({"projects": result, "root_project": root_project}), 200

        try:
            refresh_token_if_needed(user_id, "gcp")
        except Exception as e:
            logging.error(f"Token refresh failed: {e}", exc_info=True)
            return jsonify({"error": "Token refresh failed"}), 401

        token_data = get_token_data(user_id, "gcp")
        credentials = get_credentials(token_data)
        sa_email = get_aurora_service_account_email(user_id)
        projects = get_project_list(credentials)

        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
        member_sa = f"serviceAccount:{sa_email}"

        result = []
        for proj in projects:
            pid = proj.get('projectId')
            if not pid:
                continue
            name = proj.get('name', pid)

            has_permission = True
            enabled = False
            try:
                policy = crm_service.projects().getIamPolicy(resource=pid, body={}).execute()
                sa_roles = []
                for binding in policy.get('bindings', []):
                    if member_sa in binding.get('members', []):
                        sa_roles.append(binding.get('role'))
                enabled = len(sa_roles) > 0
            except Exception as e:
                logging.warning(f"Cannot read IAM policy for project {pid}: {e}")
                has_permission = False
                enabled = False

            result.append({
                "projectId": pid,
                "name": name,
                "enabled": enabled,
                "hasPermission": has_permission,
            })

        root_project = get_user_preference(user_id, 'gcp_root_project')
        for project in result:
            project['isRootProject'] = project['projectId'] == root_project

        result.sort(key=lambda x: x['name'])
        return jsonify({"projects": result, "root_project": root_project}), 200

    except ValueError as e:
        logging.warning(f"Validation error in sa_project_access_get: {e}")
        return jsonify({"error": "Invalid request parameters"}), 400
    except Exception as e:
        logging.error(f"Error in sa_project_access_get: {e}", exc_info=True)
        return jsonify({"error": "Failed to process service account project access"}), 500


@gcp_projects_bp.route("/api/gcp/sa-project-access", methods=["POST"])
@require_permission("connectors", "write")
def sa_project_access_post(user_id):
    """Update SA access based on payload {projects:[{projectId, enabled}]}."""
    try:
        token_data, err = _load_gcp_token(user_id)
        if err:
            return err

        # SA mode: nothing to persist — Aurora does not manage IAM bindings.
        if get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_SA:
            return jsonify({"success": True}), 200

        data = request.get_json() or {}
        projects = data.get("projects")
        if projects is None:
            return jsonify({"error": "projects required"}), 400

        selections = {}
        for p in projects:
            pid = p.get('projectId') or p.get('id')
            enabled = bool(p.get('enabled'))
            if pid:
                selections[pid] = enabled

        try:
            refresh_token_if_needed(user_id, "gcp")
        except Exception as e:
            logging.error(f"Token refresh failed: {e}", exc_info=True)
            return jsonify({"error": "Token refresh failed"}), 401

        token_data = get_token_data(user_id, "gcp")
        credentials = get_credentials(token_data)
        sa_email = get_aurora_service_account_email(user_id)
        update_service_account_project_access(credentials, sa_email, selections)

        return jsonify({"success": True}), 200

    except ValueError as e:
        logging.warning(f"Validation error in sa_project_access_post: {e}")
        return jsonify({"error": "Invalid request parameters"}), 400
    except Exception as e:
        logging.error(f"Error in sa_project_access_post: {e}", exc_info=True)
        return jsonify({"error": "Failed to process service account project access"}), 500
