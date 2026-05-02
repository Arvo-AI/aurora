"""GCP project management routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import get_user_preference
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_refresh import refresh_token_if_needed
from connectors.gcp_connector.auth.oauth import get_credentials
from utils.auth.token_management import get_token_data
from utils.log_sanitizer import sanitize
from connectors.gcp_connector.gcp.projects import get_project_list
from connectors.gcp_connector.auth.service_accounts import (
    get_aurora_service_account_email,
    update_service_account_project_access,
    get_gcp_auth_type,
    GCP_AUTH_TYPE_SA,
)
from connectors.gcp_connector.auth.wif import GCP_AUTH_TYPE_WIF, get_wif_access_token
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

        token_data = get_token_data(user_id, provider)
        if not token_data:
            logging.warning(f"No token data found for user_id: {user_id}, provider: {provider}")
            return jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401

        auth_type = get_gcp_auth_type(token_data)

        # SA / WIF: return pre-verified projects from stored token data
        if auth_type in (GCP_AUTH_TYPE_SA, GCP_AUTH_TYPE_WIF):
            root_project = get_user_preference(user_id, 'gcp_root_project')
            result = _sa_mode_project_list(token_data, root_project)
            return jsonify({
                "projects": result,
                "count": len(result),
                "billing_enabled_count": len(result),
                "root_project": root_project,
            }), 200

        # OAuth: refresh token, enumerate projects, check billing
        try:
            refresh_token_if_needed(user_id, provider)
        except Exception as e:
            logging.error(f"Token refresh failed: {e}", exc_info=True)
            return jsonify({"error": "Token refresh failed"}), 401

        token_data = get_token_data(user_id, provider)
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
        logging.warning("No token data found for user_id: %s, provider: gcp", sanitize(user_id))
        return None, (jsonify({"error": "No GCP credentials found. Please authenticate with GCP."}), 401)
    return token_data, None


def _refresh_and_reload_gcp_token(user_id):
    """Refresh GCP token if needed, then re-fetch token_data; return (token_data, None) or (None, error_response)."""
    try:
        refresh_token_if_needed(user_id, "gcp")
    except Exception as e:
        logging.error(f"Token refresh failed: {e}", exc_info=True)
        return None, (jsonify({"error": "Token refresh failed"}), 401)
    return get_token_data(user_id, "gcp"), None


def _sa_mode_project_list(token_data, root_project):
    """Build project list for SA/WIF mode.

    For WIF with org_id, enumerates projects live via the API.
    Otherwise returns the pre-verified accessible_projects list.
    """
    wif_config = token_data.get("wif_config") or {}
    org_id = wif_config.get("org_id")

    if org_id and get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_WIF:
        return _wif_org_project_list(token_data, org_id, root_project)

    accessible = token_data.get("accessible_projects") or []
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
    return result


def _wif_org_project_list(token_data, org_id, root_project):
    """Enumerate all org projects via WIF token."""
    result_tok = get_wif_access_token(token_data, mode="agent")
    from google.oauth2.credentials import Credentials as OAuthCreds
    creds = OAuthCreds(token=result_tok["access_token"])
    crm = build("cloudresourcemanager", "v1", credentials=creds)

    projects = []
    req = crm.projects().list(filter=f"parent.id:{org_id} lifecycleState:ACTIVE")
    while req:
        resp = req.execute()
        for p in resp.get("projects", []):
            pid = p.get("projectId")
            projects.append({
                "projectId": pid,
                "name": p.get("name") or pid,
                "enabled": True,
                "hasPermission": True,
                "isRootProject": pid == root_project,
            })
        req = crm.projects().list_next(req, resp)
    projects.sort(key=lambda x: x["name"])
    return projects


def _check_project_iam(crm_service, pid, name, member_sa, root_project):
    """Check IAM access for one project; return project info dict."""
    has_permission = True
    enabled = False
    try:
        policy = crm_service.projects().getIamPolicy(resource=pid, body={}).execute()
        for binding in policy.get('bindings', []):
            if member_sa in binding.get('members', []):
                enabled = True
                break
    except Exception as e:
        logging.warning(f"Cannot read IAM policy for project {pid}: {e}")
        has_permission = False
        enabled = False

    return {
        "projectId": pid,
        "name": name,
        "enabled": enabled,
        "hasPermission": has_permission,
        "isRootProject": pid == root_project,
    }


def _oauth_mode_project_list(credentials, sa_email, root_project):
    """Build project list for OAuth mode by enumerating projects + IAM."""
    projects = get_project_list(credentials)
    crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
    member_sa = f"serviceAccount:{sa_email}"

    result = []
    for proj in projects:
        pid = proj.get('projectId')
        if not pid:
            continue
        result.append(_check_project_iam(
            crm_service, pid, proj.get('name', pid), member_sa, root_project,
        ))
    result.sort(key=lambda x: x['name'])
    return result


@gcp_projects_bp.route("/api/gcp/sa-project-access", methods=["GET"])
@require_permission("connectors", "read")
def sa_project_access_get(user_id):
    """List projects with SA access flag."""
    try:
        token_data, err = _load_gcp_token(user_id)
        if err:
            return err

        root_project = get_user_preference(user_id, 'gcp_root_project')

        # SA / WIF mode: surface pre-verified accessible_projects (no IAM enumeration).
        auth_type = get_gcp_auth_type(token_data)
        if auth_type in (GCP_AUTH_TYPE_SA, GCP_AUTH_TYPE_WIF):
            result = _sa_mode_project_list(token_data, root_project)
            return jsonify({"projects": result, "root_project": root_project}), 200

        token_data, err = _refresh_and_reload_gcp_token(user_id)
        if err:
            return err

        credentials = get_credentials(token_data)
        sa_email = get_aurora_service_account_email(user_id)
        result = _oauth_mode_project_list(credentials, sa_email, root_project)
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

        # SA / WIF mode: nothing to persist — Aurora does not manage IAM bindings.
        if get_gcp_auth_type(token_data) in (GCP_AUTH_TYPE_SA, GCP_AUTH_TYPE_WIF):
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

        token_data, err = _refresh_and_reload_gcp_token(user_id)
        if err:
            return err

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
