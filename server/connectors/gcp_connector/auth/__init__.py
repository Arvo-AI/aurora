"""
Authentication module for user's own GCP credentials.
"""

from .oauth import (
    get_auth_url,
    exchange_code_for_token,
    get_credentials,
    refresh_token_if_needed,
)

from utils.auth.token_management import (
    store_tokens_in_db,
    get_token_data,
)

from .service_accounts import (
    ensure_aurora_full_access,
    generate_sa_access_token,
    get_aurora_service_account_email,
    update_service_account_project_access,
    create_local_credentials_file,
)
from ..gcp.projects import (
    get_project_list,
)

__all__ = [
    # OAuth functions
    'get_auth_url',
    'exchange_code_for_token',
    'get_credentials',
    'refresh_token_if_needed',
    # Token storage
    'store_tokens_in_db',
    'get_token_data',
    # Service accounts
    'ensure_aurora_full_access',
    'generate_sa_access_token',
    'get_aurora_service_account_email',
    'update_service_account_project_access',
    'create_local_credentials_file',
    # Project functions
    'get_project_list',
]

