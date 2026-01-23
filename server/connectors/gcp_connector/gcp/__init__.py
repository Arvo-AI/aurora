"""
GCP operations module for user's own Google Cloud Platform projects.
"""

from .apis import (
    AURORA_REQUIRED_APIS,
    wait_for_operation,
    enable_single_api,
    enable_apis_batch,
    enable_apis_for_all_projects,
    check_api_enabled,
    list_enabled_apis,
)

from .projects import (
    get_project_list,
    list_gke_clusters,
    check_billing_enabled,
    select_best_project,
    get_project_details,
    list_projects_with_filter,
    get_organization_id,
)

from .iam import (
    add_binding_if_missing,
    remove_binding_member,
    set_project_bindings,
    remove_project_bindings,
    set_org_bindings,
    set_service_account_policy,
    allow_public_access_iam_policy,
    allow_public_access_for_all_projects,
)

__all__ = [
    # APIs
    'AURORA_REQUIRED_APIS',
    'wait_for_operation',
    'enable_single_api',
    'enable_apis_batch',
    'enable_apis_for_all_projects',
    'check_api_enabled',
    'list_enabled_apis',
    # Projects
    'get_project_list',
    'list_gke_clusters',
    'check_billing_enabled',
    'select_best_project',
    'get_project_details',
    'list_projects_with_filter',
    'get_organization_id',
    # IAM
    'add_binding_if_missing',
    'remove_binding_member',
    'set_project_bindings',
    'remove_project_bindings',
    'set_org_bindings',
    'set_service_account_policy',
    'allow_public_access_iam_policy',
    'allow_public_access_for_all_projects',
]

