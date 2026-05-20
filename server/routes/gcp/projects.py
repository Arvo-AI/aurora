"""GCP project management routes."""
import hashlib
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
from connectors.gcp_connector.billing import has_active_billing
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from utils.secrets.secret_ref_utils import get_token_owner_id

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
    """Build project list for service-account mode (no IAM enumeration)."""
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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from googleapiclient._helpers import positional

    projects = get_project_list(credentials)
    member_sa = f"serviceAccount:{sa_email}"

    items = [(p.get('projectId'), p.get('name', p.get('projectId'))) for p in projects if p.get('projectId')]

    def check_one(pid_name):
        pid, name = pid_name
        # Each thread needs its own client (httplib2 isn't thread-safe)
        crm = build('cloudresourcemanager', 'v1', credentials=credentials, cache_discovery=False)
        return _check_project_iam(crm, pid, name, member_sa, root_project)

    result = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(check_one, item): item for item in items}
        for future in as_completed(futures):
            try:
                result.append(future.result())
            except Exception as e:
                pid, name = futures[future]
                logging.warning(f"IAM check failed for {pid}: {e}")
                result.append({"projectId": pid, "name": name, "enabled": False, "hasPermission": False, "isRootProject": pid == root_project})

    result.sort(key=lambda x: x['name'])
    return result


def _derive_sa_email(owner_id: str, root_project_id: str) -> str:
    """Derive Aurora SA email from owner_id and root project without any API calls.

    The email format is deterministic:
        aurora-{sha256(owner_id)[:20]}-f@{root_project_id}.iam.gserviceaccount.com
    """
    hash_part = hashlib.sha256(owner_id.encode('utf-8')).hexdigest()[:20]
    return f"aurora-{hash_part}-f@{root_project_id}.iam.gserviceaccount.com"


@gcp_projects_bp.route("/api/gcp/sa-project-access", methods=["GET"])
@require_permission("connectors", "read")
def sa_project_access_get(user_id):
    """List projects with SA access flag."""
    try:
        token_data, err = _load_gcp_token(user_id)
        if err:
            return err

        root_project = get_user_preference(user_id, 'gcp_root_project')

        # SA mode: surface auto-discovered accessible_projects (no IAM enumeration).
        # Aurora doesn't manage IAM bindings in SA mode — the uploaded SA already
        # has whatever roles the user granted it directly in GCP.
        if get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_SA:
            result = _sa_mode_project_list(token_data, root_project)
            return jsonify({"projects": result, "root_project": root_project}), 200

        # Resolve SA owner before refresh — after a refresh the token may be
        # stored under user_id (creating a duplicate row), which would cause
        # get_token_owner_id to return user_id (wrong SA hash) instead of the
        # original connector owner's ID.
        sa_owner_id = get_token_owner_id(user_id, "gcp")

        token_data, err = _refresh_and_reload_gcp_token(user_id)
        if err:
            return err

        credentials = get_credentials(token_data)

        # Derive SA email directly when root_project is known (zero API calls).
        # get_aurora_service_account_email() lists all projects + checks billing/IAM
        # per project sequentially — catastrophically slow for 100+ projects.
        sa_setup_pending = False
        if root_project:
            sa_email = _derive_sa_email(sa_owner_id, root_project)

            # Check if SA actually exists — if not, fall back to listing
            # projects without IAM checks (setup is still in progress).
            iam_service = build('iam', 'v1', credentials=credentials)
            sa_resource = f"projects/{root_project}/serviceAccounts/{sa_email}"
            try:
                iam_service.projects().serviceAccounts().get(name=sa_resource).execute()
                result = _oauth_mode_project_list(credentials, sa_email, root_project)
            except HttpError as e:
                if e.resp.status == 404:
                    logging.info("SA %s not yet provisioned, listing without IAM checks", sa_email)
                    sa_setup_pending = True
                    projects = get_project_list(credentials)
                    result = []
                    for p in projects:
                        pid = p.get('projectId')
                        if not pid:
                            continue
                        result.append({
                            "projectId": pid,
                            "name": p.get('name', pid),
                            "enabled": False,
                            "hasPermission": True,
                            "isRootProject": pid == root_project,
                        })
                    result.sort(key=lambda x: x['name'])
                else:
                    raise
        else:
            # No root project means no SA has been provisioned yet.
            # Just list projects without IAM checks (all will show enabled=False).
            sa_setup_pending = True
            projects = get_project_list(credentials)
            result = []
            for p in projects:
                pid = p.get('projectId')
                if not pid:
                    continue
                result.append({
                    "projectId": pid,
                    "name": p.get('name', pid),
                    "enabled": False,
                    "hasPermission": True,
                    "isRootProject": False,
                })
            result.sort(key=lambda x: x['name'])

        return jsonify({
            "projects": result,
            "root_project": root_project,
            "sa_setup_pending": sa_setup_pending,
        }), 200

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

        # Resolve SA owner before refresh — refresh may create a token row
        # under user_id (org-member), which would make get_token_owner_id
        # return the wrong ID if called after.
        sa_owner_id = get_token_owner_id(user_id, "gcp")

        token_data, err = _refresh_and_reload_gcp_token(user_id)
        if err:
            return err

        credentials = get_credentials(token_data)

        root_project = get_user_preference(user_id, 'gcp_root_project')
        if not root_project:
            # No root project yet — auto-select the first project being
            # enabled as the root, trigger SA creation, and tell the frontend
            # to retry after setup completes.
            first_enabled = next((pid for pid, en in selections.items() if en), None)
            if not first_enabled:
                return jsonify({"error": "No root project configured and no project being enabled."}), 400
            from utils.auth.stateless_auth import store_user_preference
            store_user_preference(user_id, 'gcp_root_project', first_enabled)
            from routes.gcp.root_project_tasks import setup_root_project_async
            setup_root_project_async.delay(user_id, first_enabled)
            logging.info("Auto-selected root project %s for user %s, triggered SA setup", first_enabled, user_id)
            return jsonify({
                "success": True,
                "root_project_set": first_enabled,
                "sa_setup_pending": True,
                "message": "Root project set and service account provisioning started."
            }), 202

        sa_email = _derive_sa_email(sa_owner_id, root_project)

        # Verify the SA actually exists before attempting IAM operations.
        # The SA is created asynchronously by setup_root_project_async — if it
        # hasn't completed yet, setIamPolicy will fail with a confusing 400.
        iam_service = build('iam', 'v1', credentials=credentials)
        sa_resource = f"projects/{root_project}/serviceAccounts/{sa_email}"
        try:
            iam_service.projects().serviceAccounts().get(name=sa_resource).execute()
        except HttpError as e:
            if e.resp.status == 404:
                return jsonify({
                    "success": True,
                    "sa_setup_pending": True,
                    "message": "Service account is being provisioned. Projects will be configured once setup completes."
                }), 202
            raise

        update_service_account_project_access(credentials, sa_email, selections)

        return jsonify({"success": True}), 200

    except ValueError as e:
        logging.warning(f"Validation error in sa_project_access_post: {e}")
        return jsonify({"error": "Invalid request parameters"}), 400
    except Exception as e:
        logging.error(f"Error in sa_project_access_post: {e}", exc_info=True)
        return jsonify({"error": "Failed to process service account project access"}), 500
