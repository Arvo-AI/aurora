import logging

# Import functions from helper modules instead of defining them locally
from connectors.gcp_connector.gcp.projects import (
    get_project_list,
    select_best_project as _select_root_project,
    get_organization_id as _get_org_id,
)
from connectors.gcp_connector.gcp.iam import (
    add_binding_if_missing as _add_binding_if_missing,
    set_project_bindings as _set_project_bindings,
    set_org_bindings as _set_org_bindings,
    set_service_account_policy as _set_sa_policy,
    allow_public_access_iam_policy,
    allow_public_access_for_all_projects,
)
from connectors.gcp_connector.auth.service_accounts import (
    ensure_aurora_full_access,
    update_service_account_project_access,
)
from connectors.gcp_connector.gcp.apis import (
    enable_single_api,
    enable_apis_batch,
    enable_apis_for_all_projects,
    AURORA_REQUIRED_APIS,
)

__all__ = [
    "get_project_list",
    "enable_artifact_registry_api",
    "enable_artifact_registry_for_all_projects",
    "enable_cloud_run_admin_api",
    "enable_cloud_run_admin_for_all_projects",
    "enable_all_required_apis_for_all_projects",
    "enable_required_apis_for_project",
    "_add_binding_if_missing",
    "_set_project_bindings",
    "_set_org_bindings",
    "_set_sa_policy",
    "_get_org_id",
    "_select_root_project",
    "ensure_aurora_full_access",
    "update_service_account_project_access",
    "allow_public_access_iam_policy",
    "allow_public_access_for_all_projects",
]

# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

# get_project_list is imported from gcp.projects

# ---------------------------------------------------------------------------
# API enablement helpers (Artifact Registry, Cloud Run, batch enablement)
# ---------------------------------------------------------------------------

def enable_artifact_registry_api(credentials, project_id: str) -> bool:
    """Enable the Artifact Registry API for a single project."""
    success, error = enable_single_api(credentials, project_id, "artifactregistry.googleapis.com")
    if not success:
        logging.error(f"Artifact Registry enablement failed: {error}")
    return success

def enable_artifact_registry_for_all_projects(credentials):
    """Enable Artifact Registry API for all accessible projects."""
    return enable_apis_for_all_projects(credentials, ["artifactregistry.googleapis.com"])

# Cloud Run Admin API helpers

def enable_cloud_run_admin_api(credentials, project_id: str) -> bool:
    """Enable the Cloud Run Admin API for a single project."""
    success, error = enable_single_api(credentials, project_id, "run.googleapis.com")
    if not success:
        logging.error(f"Cloud Run API enablement failed: {error}")
    return success

def enable_cloud_run_admin_for_all_projects(credentials):
    """Enable Cloud Run Admin API for all accessible projects."""
    return enable_apis_for_all_projects(credentials, ["run.googleapis.com"])

# Batch enable a required list of APIs

def enable_required_apis_for_project(credentials, project_id: str) -> bool:
    """Enable all required APIs for a single project.
    
    This is a backward compatibility wrapper around enable_apis_batch.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        
    Returns:
        bool: True if successful, False otherwise
    """
    success, error = enable_apis_batch(credentials, project_id, AURORA_REQUIRED_APIS)
    if not success:
        logging.error(f"Failed to enable required APIs for project {project_id}: {error}")
    return success

def enable_all_required_apis_for_all_projects(credentials, projects=None):
    """Enable all required APIs for all accessible projects.
    
    Args:
        credentials: Google OAuth credentials object
        projects: Optional list of project dicts to process. If None, fetches all projects.
    """
    # Use the default AURORA_REQUIRED_APIS from gcp.apis
    return enable_apis_for_all_projects(credentials, projects=projects)