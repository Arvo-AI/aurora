"""
Backwards compatibility layer for auth.py migration.
This file provides import mappings for the old auth.py functions to the new modular structure.
"""

# OAuth and authentication
from connectors.gcp_connector.auth.oauth import (
    get_auth_url,
    exchange_code_for_token,
    get_credentials,
    refresh_token_if_needed,
)

# Token storage
from utils.auth.token_management import (
    store_tokens_in_db,
    get_token_data,
)

# Service accounts
from connectors.gcp_connector.auth.service_accounts import (
    ensure_aurora_full_access,
    generate_sa_access_token,
    get_aurora_service_account_email,
    update_service_account_project_access,
    create_local_credentials_file,
)

# GCP Projects
from connectors.gcp_connector.gcp.projects import (
    get_project_list,
    list_gke_clusters,
)

# GCP APIs - Import from aurora_project_access for compatibility
from connectors.gcp_connector.aurora_project_access import (
    enable_cloud_run_admin_api,
    enable_cloud_run_admin_for_all_projects,
    enable_all_required_apis_for_all_projects,
    enable_required_apis_for_project,
)

# GCP IAM
from connectors.gcp_connector.gcp.iam import (
    allow_public_access_iam_policy,
    allow_public_access_for_all_projects,
)

# Aurora tokens
from utils.auth.cloud_auth import (
    get_provider_preference_from_context,
    generate_contextual_access_token,
)

# Import wrapper functions directly from aurora_project_access for backward compatibility
from connectors.gcp_connector.aurora_project_access import (
    enable_artifact_registry_api,
    enable_artifact_registry_for_all_projects,
)


# For _select_root_project - this is now in gcp.projects as select_best_project
from connectors.gcp_connector.gcp.projects import select_best_project as _select_root_project

# Export all the functions to maintain backwards compatibility
__all__ = [
    # OAuth
    'get_auth_url',
    'exchange_code_for_token',
    'get_credentials',
    'refresh_token_if_needed',
    # Tokens
    'store_tokens_in_db',
    'get_token_data',
    # Service Accounts
    'ensure_aurora_full_access',
    'generate_sa_access_token',
    'get_aurora_service_account_email',
    'update_service_account_project_access',
    'create_local_credentials_file',
    # Projects
    'get_project_list',
    'list_gke_clusters',
    # APIs
    'enable_artifact_registry_api',
    'enable_artifact_registry_for_all_projects',
    'enable_cloud_run_admin_api',
    'enable_cloud_run_admin_for_all_projects',
    'enable_all_required_apis_for_all_projects',
    'enable_required_apis_for_project',
    # IAM
    'allow_public_access_iam_policy',
    'allow_public_access_for_all_projects',
    # Token helpers
    'get_provider_preference_from_context',
    'generate_contextual_access_token',
    # Internal
    '_select_root_project',
]

