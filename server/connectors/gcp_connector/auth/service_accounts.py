"""
Service account management and impersonation for user's GCP projects.
"""

import logging
import json
import os
import time
import tempfile
import datetime
import hashlib
from typing import List, Dict, Optional, Tuple
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from connectors.gcp_connector.gcp.project_selection import (
    ProjectSelectionStrategy,
    DefaultProjectSelectionStrategy,
)

logger = logging.getLogger(__name__)


def _get_user_sa_suffix(user_id: str, sa_type: str = 'full') -> str:
    """Generate a stable, short hash from user_id for SA naming.

    GCP service account IDs have a 30-char limit. We use:
    - 'aurora-' prefix (7 chars)
    - user hash (20 chars from SHA256)
    - type suffix (2 chars): '-f' for full access, '-r' for read-only
    = 29 chars total

    Args:
        user_id: User identifier
        sa_type: 'full' for full-access SA, 'readonly' for read-only SA

    Returns:
        22-character string: hash (20 chars) + suffix (2 chars)
    """
    hash_obj = hashlib.sha256(user_id.encode('utf-8'))
    hash_part = hash_obj.hexdigest()[:20]

    suffix = '-f' if sa_type == 'full' else '-r'
    return f"{hash_part}{suffix}"


FULL_ACCESS_RUNNER_ID = "aurora-tool-runner"
READ_ONLY_RUNNER_ID = "aurora-readonly-runner"
READ_ONLY_PROJECT_ROLES = [
    'roles/iam.serviceAccountTokenCreator',
    'roles/iam.serviceAccountUser',
    'roles/viewer',
    'roles/logging.viewer',
    'roles/monitoring.viewer',
    'roles/browser',  # Provides read access to browse project hierarchy
    'roles/cloudasset.viewer',
    'roles/compute.viewer',
    'roles/container.viewer',
    'roles/storage.objectViewer',
]


def ensure_aurora_full_access(
    credentials,
    user_id: str,
    projects_list: Optional[List[dict]] = None,
    root_project_id_override: Optional[str] = None,
) -> bool:
    """Replicate the shell installer logic using the GCP Python SDK.
    
    This will:
    1. Select the best root project (recently used > billing enabled > first).
    2. Ensure the `aurora-tool-runner` service account exists in the root project.
    3. Grant OWNER, ServiceAccountUser, and TokenCreator roles to that SA on all accessible projects (or only selected projects if provided).
    4. Grant the currently authenticated user ServiceAccountAdmin + TokenCreator on the root project.
    5. Allow the user to impersonate the SA (TokenCreator role on the SA itself).
    
    The function is idempotent – it will only add bindings that are missing.
    
    Args:
        credentials: Google OAuth credentials object
        user_id: User identifier
        projects_list: Optional list of project dicts to setup. If None, will fetch all accessible projects.
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        from connectors.gcp_connector.gcp.projects import get_project_list, get_organization_id
        from connectors.gcp_connector.gcp.iam import (
            set_project_bindings, set_org_bindings, set_service_account_policy
        )
        
        # Build client libraries
        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
        iam_service = build('iam', 'v1', credentials=credentials)
        oauth2_service = build('oauth2', 'v2', credentials=credentials)
        
        # Identify user email (used for impersonation binding)
        user_email = None
        try:
            user_info = oauth2_service.userinfo().get().execute()
            user_email = user_info.get('email')
            logger.info(f"Successfully fetched user email: {user_email}")
        except Exception as e:
            logger.warning(f"Unable to fetch user email: {e}")
        
        if not user_email:
            raise ValueError("Unable to determine user email address. Please ensure your Google account is properly authenticated.")
        
        # Get project list (use provided list or fetch all)
        if projects_list is None:
            projects = get_project_list(credentials)
        else:
            projects = projects_list
            
        if not projects:
            logger.warning("No GCP projects discovered – skipping SA setup")
            return False
        
        # To fix bug where would create SA in the wrong project and then rest of auth 
        # would fail because would look for the SA in the wrong project
        if root_project_id_override:
            root_project_id = root_project_id_override
            logger.info(f"Using provided root project override: {root_project_id}")
        else:
            root_project_id = DefaultProjectSelectionStrategy().determine(credentials, projects, user_id)

        if not root_project_id:
            logger.warning("Could not determine root project – aborting SA setup")
            return False
        
        # Try to grant user the required IAM roles if they don't already have them
        if user_email:
            required_user_roles = [
                'roles/iam.serviceAccountAdmin',
                'roles/iam.serviceAccountTokenCreator'
            ]
            
            try:
                set_project_bindings(
                    crm_service,
                    root_project_id,
                    f"user:{user_email}",
                    required_user_roles
                )
                logger.info(f"Successfully granted required roles to user {user_email}")
            except HttpError as e:
                if e.resp.status == 403:
                    error_msg = (
                        f"Insufficient permissions to grant required roles to user {user_email} on project {root_project_id}. "
                        f"You need one of the following:\n"
                        f"1. Owner role on the project, OR\n"
                        f"2. IAM Manager role (roles/resourcemanager.projectIamAdmin), OR\n"
                        f"3. Both 'Service Account Admin' and 'Service Account Token Creator' roles already assigned\n\n"
                        f"You can grant these roles in the GCP Console: "
                        f"https://console.cloud.google.com/iam-admin/iam?project={root_project_id}"
                    )
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                else:
                    logger.error(f"Unexpected error granting roles: {e}")
                    raise ValueError(f"Failed to grant required permissions: {str(e)}")
        else:
            logger.warning("Skipping user IAM role assignment - could not determine user email.")
        
        sa_email, sa_resource = _ensure_service_account(
            iam_service,
            root_project_id,
            FULL_ACCESS_RUNNER_ID,
            'Aurora Tool Runner',
            'Primary Aurora service account for Agent mode',
            user_id=user_id,
            sa_type='full',
        )
        
        # 3) Attempt org-level binding first
        org_name = get_organization_id(credentials)
        member_sa = f"serviceAccount:{sa_email}"
        roles_for_sa = [
            'roles/owner',
            'roles/iam.serviceAccountUser',
            'roles/iam.serviceAccountTokenCreator'
        ]
        if org_name:
            set_org_bindings(crm_service, org_name, member_sa, roles_for_sa)
        else:
            logger.info("No organisation context – will grant per-project roles")
        
        # 4) Ensure SA has roles on each project
        for project in projects:
            pid = project.get('projectId')
            if not pid:
                continue
            set_project_bindings(crm_service, pid, member_sa, roles_for_sa)
        
        # 5) Add impersonation binding on the service account for the user
        if user_email:
            set_service_account_policy(iam_service, sa_resource, f"user:{user_email}")
        else:
            logger.warning("Skipping service account impersonation setup - could not determine user email.")

        # Provision the Ask-mode runner with read-only roles
        read_only_sa_email = _ensure_read_only_runner(
            iam_service=iam_service,
            crm_service=crm_service,
            set_project_bindings_fn=set_project_bindings,
            set_service_account_policy_fn=set_service_account_policy,
            projects=projects,
            root_project_id=root_project_id,
            user_email=user_email,
            full_access_sa_email=sa_email,
            user_id=user_id,
        )

        logger.info(f"Aurora full-access setup completed successfully")
        logger.info(f"Root project: {root_project_id}")
        logger.info(f"Agent service account: {sa_email}")
        logger.info(f"Read-only service account: {read_only_sa_email}")
        logger.info(f"Granted permissions on {len(projects)} projects")
        return True
    except ValueError:
        # Re-raise ValueError with clear user messages
        raise
    except Exception as e:
        logger.error(f"Error during Aurora full-access setup: {e}")
        error_msg = (
            f"Unexpected error during Aurora setup: {str(e)}. "
            f"Please check your GCP permissions and try again."
        )
        raise ValueError(error_msg)


def _ensure_service_account(iam_service, project_id: str, account_id: str, display_name: str,
                            description: Optional[str] = None, user_id: Optional[str] = None,
                            sa_type: str = 'full') -> Tuple[str, str]:
    """Create the requested service account if it does not already exist.

    Args:
        iam_service: IAM service client
        project_id: GCP project ID where SA will be created
        account_id: Base account ID (e.g., 'aurora-tool-runner')
        display_name: Human-readable display name
        description: Optional description
        user_id: User identifier - if provided, creates user-specific SA
        sa_type: 'full' for full-access SA, 'readonly' for read-only SA

    Returns:
        Tuple of (sa_email, sa_resource)
    """
    import time

    # If user_id provided, create user-specific SA name
    if user_id:
        user_suffix = _get_user_sa_suffix(user_id, sa_type)
        account_id = f"aurora-{user_suffix}"

    sa_email = f"{account_id}@{project_id}.iam.gserviceaccount.com"
    sa_resource = f"projects/{project_id}/serviceAccounts/{sa_email}"

    try:
        iam_service.projects().serviceAccounts().get(name=sa_resource).execute()
        logger.info("Service account %s already exists", sa_email)
        return sa_email, sa_resource
    except HttpError as exc:
        if exc.resp.status != 404:
            logger.error("Unexpected error checking SA %s: %s", sa_email, exc)
            raise ValueError(f"Failed to check service account existence: {exc}")

        body = {
            'accountId': account_id,
            'serviceAccount': {
                'displayName': display_name,
            },
        }
        if description:
            body['serviceAccount']['description'] = description

        logger.info("Creating service account %s", sa_email)
        try:
            iam_service.projects().serviceAccounts().create(
                name=f"projects/{project_id}",
                body=body,
            ).execute()

            # Simple wait for IAM propagation
            logger.info("Waiting for service account propagation...")
            time.sleep(5)

        except HttpError as create_error:
            if create_error.resp.status == 409:
                # Service account was created concurrently, that's OK
                logger.info("Service account %s already exists", sa_email)
                time.sleep(3)
            else:
                error_msg = f"Failed to create service account {sa_email}: {create_error}"
                logger.error(error_msg)
                raise ValueError(error_msg)

    return sa_email, sa_resource


def _ensure_read_only_runner(
    *,
    iam_service,
    crm_service,
    set_project_bindings_fn,
    set_service_account_policy_fn,
    projects: List[Dict[str, str]],
    root_project_id: str,
    user_email: Optional[str],
    full_access_sa_email: Optional[str],
    user_id: Optional[str] = None,
) -> str:
    """Provision the Ask-mode runner SA with viewer permissions across projects."""

    sa_email, sa_resource = _ensure_service_account(
        iam_service,
        root_project_id,
        READ_ONLY_RUNNER_ID,
        "Aurora ReadOnly Runner",
        "Read-only service account for Aurora Ask mode",
        user_id=user_id,
        sa_type='readonly',
    )

    member_sa = f"serviceAccount:{sa_email}"
    target_project_ids = {root_project_id}
    for project in projects:
        pid = project.get('projectId')
        if pid:
            target_project_ids.add(pid)

    # Critical: Apply roles to the service account - this MUST succeed
    roles_applied = False
    for pid in sorted(target_project_ids):
        try:
            set_project_bindings_fn(crm_service, pid, member_sa, READ_ONLY_PROJECT_ROLES)
            logger.info("Granted read-only roles %s to %s on project %s", READ_ONLY_PROJECT_ROLES, sa_email, pid)
            roles_applied = True
        except Exception as err:
            error_msg = f"Failed to grant read-only roles on project {pid}: {err}"
            logger.error(error_msg)
            # For the root project, this is critical - fail loudly
            if pid == root_project_id:
                raise ValueError(f"Critical: Cannot grant roles to read-only SA on root project {pid}: {err}")
            # For other projects, log but continue
            logger.warning("Continuing despite role grant failure on non-root project %s", pid)

    if not roles_applied:
        raise ValueError(f"Failed to apply roles to read-only service account {sa_email} on any project")

    if user_email:
        set_service_account_policy_fn(iam_service, sa_resource, f"user:{user_email}")
    else:
        logger.warning("Skipping read-only SA impersonation binding - could not determine user email.")

    if full_access_sa_email:
        set_service_account_policy_fn(
            iam_service,
            sa_resource,
            f"serviceAccount:{full_access_sa_email}",
        )
        logger.info(
            "Granted TokenCreator on %s to peer service account %s",
            sa_email,
            full_access_sa_email,
        )

    return sa_email


def _service_account_exists(iam_service, project_id: str, sa_email: str) -> bool:
    """Return True if the given service account exists inside project_id."""
    sa_resource = f"projects/{project_id}/serviceAccounts/{sa_email}"
    try:
        iam_service.projects().serviceAccounts().get(name=sa_resource).execute()
        return True
    except HttpError as exc:
        if exc.resp.status == 404:
            return False
        logger.error("Failed to verify service account %s: %s", sa_email, exc)
        raise ValueError(f"Failed to verify service account {sa_email}: {exc}") from exc


def generate_sa_access_token(user_id: str, scopes: List[str] = None, 
                            lifetime: int = 3600, selected_project_id: str = None,
                            mode: Optional[str] = None) -> Dict:
    """Generate a short-lived access token by impersonating the Aurora service account.
    
    Args:
        user_id: The user ID for authentication
        scopes: Token scopes (defaults to cloud-platform)
        lifetime: Token lifetime in seconds
        selected_project_id: Optional specific project ID to use
        
    Returns:
        Dict with token, expiry, project_id, sa_email
    """
    if scopes is None:
        # Use only cloud-platform scope for service account impersonation
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    normalized_mode = (mode or '').strip().lower()
    
    from utils.auth.token_management import get_token_data
    from connectors.gcp_connector.auth.oauth import get_credentials
    from connectors.gcp_connector.gcp.projects import get_project_list, select_best_project
    
    token_data = get_token_data(user_id, "gcp")
    if not token_data:
        raise ValueError("No GCP token data for user")
    
    user_creds = get_credentials(token_data)
    iam_service = build('iam', 'v1', credentials=user_creds)
    
    projects = get_project_list(user_creds)
    if not projects:
        raise ValueError("User has no accessible GCP projects")
    
    # Always use the root project for the service account (where it was created)
    root_project_id = select_best_project(user_creds, projects, user_id)

    # Generate user-specific SA name (default to full-access)
    user_suffix_full = _get_user_sa_suffix(user_id, 'full')
    user_sa_id_full = f"aurora-{user_suffix_full}"
    sa_email = f"{user_sa_id_full}@{root_project_id}.iam.gserviceaccount.com"

    if normalized_mode == 'ask':
        # Try to use read-only SA if it exists
        user_suffix_readonly = _get_user_sa_suffix(user_id, 'readonly')
        user_sa_id_readonly = f"aurora-{user_suffix_readonly}"
        read_only_email = f"{user_sa_id_readonly}@{root_project_id}.iam.gserviceaccount.com"
        try:
            if _service_account_exists(iam_service, root_project_id, read_only_email):
                sa_email = read_only_email
                logger.info("Using read-only runner service account for user %s", user_id)
            else:
                logger.warning(
                    "Read-only service account %s missing in project %s; falling back to full-access runner",
                    read_only_email,
                    root_project_id,
                )
        except ValueError as err:
            logger.warning(
                "Unable to verify read-only service account for project %s: %s. Falling back to full-access runner.",
                root_project_id,
                err,
            )
    
    # Determine which project to return for queries
    target_project_id = root_project_id  # Default to root project
    if selected_project_id:
        # Verify the selected project exists in user's project list
        project_ids = [p.get('projectId') for p in projects]
        if selected_project_id in project_ids:
            target_project_id = selected_project_id
            logger.info(f"Using user-selected project for queries: {selected_project_id}")
        else:
            logger.info(f"Selected project {selected_project_id} not accessible. Using root project: {root_project_id}")
    else:
        logger.info(f"No project selected, using root project for queries: {root_project_id}")
    
    # First, check if the service account exists
    try:
        iam_service = build('iam', 'v1', credentials=user_creds)
        sa_resource = f"projects/{root_project_id}/serviceAccounts/{sa_email}"
        iam_service.projects().serviceAccounts().get(name=sa_resource).execute()
        logger.info(f"Service account {sa_email} exists")
    except HttpError as e:
        if e.resp.status == 404:
            raise ValueError(f"Service account {sa_email} does not exist. Please run the setup process first.")
        else:
            raise ValueError(f"Error checking service account existence: {e}")
    
    # Check if user has impersonation permission
    try:
        iam_service.projects().serviceAccounts().testIamPermissions(
            resource=sa_resource,
            body={'permissions': ['iam.serviceAccounts.getAccessToken']}
        ).execute()
        logger.info(f"User has impersonation permission for {sa_email}")
    except Exception as e:
        logger.warning(f"Could not verify impersonation permission: {e}")
    
    # Generate the access token
    try:
        iamcred = build("iamcredentials", "v1", credentials=user_creds)
        
        logger.info(f"Requesting access token for service account: {sa_email}")
        logger.info(f"Scopes: {scopes}")
        logger.info(f"Lifetime: {lifetime}s")
        
        resp = iamcred.projects().serviceAccounts().generateAccessToken(
            name=f"projects/-/serviceAccounts/{sa_email}",
            body={
                "scope": scopes,
                "lifetime": f"{lifetime}s"
            },
        ).execute()
        
        return {
            "access_token": resp["accessToken"],
            "expire_time": resp["expireTime"],
            "project_id": target_project_id,  # Return the target project for queries
            "service_account_email": sa_email,
        }
    except HttpError as e:
        logger.error(f"Failed to generate access token: {e}")
        logger.error(f"Service account: {sa_email}")
        logger.error(f"Root project ID: {root_project_id}")
        logger.error(f"Target project ID: {target_project_id}")
        raise


def get_aurora_service_account_email(user_id: str) -> str:
    """Get the preferred Aurora service account email for the user.

    Returns the service account email from the root project based on the
    project selection logic (user preference > billing enabled > first).

    Args:
        user_id: User identifier

    Returns:
        Service account email string
    """
    from utils.auth.token_management import get_token_data
    from connectors.gcp_connector.auth.oauth import get_credentials
    from connectors.gcp_connector.gcp.projects import get_project_list, select_best_project

    token_data = get_token_data(user_id, "gcp")
    if not token_data:
        raise ValueError("No GCP token data for user")

    user_creds = get_credentials(token_data)
    projects = get_project_list(user_creds)
    if not projects:
        raise ValueError("User has no accessible GCP projects")

    root_project_id = select_best_project(user_creds, projects, user_id)

    # Generate user-specific SA email
    user_suffix = _get_user_sa_suffix(user_id, 'full')
    user_sa_id = f"aurora-{user_suffix}"
    return f"{user_sa_id}@{root_project_id}.iam.gserviceaccount.com"


def update_service_account_project_access(credentials, sa_email: str, selections: Dict[str, bool]):
    """Synchronise service-account access based on user selections.
    
    Args:
        credentials: Authenticated user credentials
        sa_email: The Aurora service-account email
        selections: Mapping of project_id → bool (True = should have access)
    """
    from connectors.gcp_connector.gcp.iam import set_project_bindings, remove_project_bindings
    
    crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
    member_sa = f"serviceAccount:{sa_email}"
    roles_for_sa = [
        'roles/owner',
        'roles/iam.serviceAccountUser',
        'roles/iam.serviceAccountTokenCreator',
    ]
    
    for project_id, enabled in selections.items():
        if enabled:
            set_project_bindings(crm_service, project_id, member_sa, roles_for_sa)
        else:
            remove_project_bindings(crm_service, project_id, member_sa, roles_for_sa)


def verify_project_access(user_id: str, project_id: str) -> dict:
    """
    Verify SA access to specific project by attempting token generation.

    Returns:
        {'accessible': bool, 'error': str|None, 'sa_email': str|None}
    """
    try:
        token_data = generate_sa_access_token(user_id, selected_project_id=project_id)
        return {
            'accessible': True,
            'sa_email': token_data['service_account_email'],
            'error': None
        }
    except Exception as e:
        logger.warning(f"Verification failed for project {project_id}: {str(e)}")
        return {
            'accessible': False,
            'error': str(e),
            'sa_email': None
        }

def create_local_credentials_file(token_data, project_id: str) -> str:
    """Create a temporary file with GCP OAuth credentials.
    
    Args:
        token_data: OAuth token data
        project_id: GCP project ID
    
    Returns:
        str: Path to the credentials file
    """
    from connectors.gcp_connector.auth.oauth import refresh_token_if_needed, CLIENT_ID, CLIENT_SECRET, TOKEN_URL
    
    try:
        # Check if we have a refresh token before attempting refresh
        if not token_data.get('refresh_token'):
            raise ValueError("No refresh token available for credentials file creation")
        
        # Make sure token is up-to-date
        success, updated_token_data = refresh_token_if_needed(token_data)
        if not success:
            raise ValueError("Failed to refresh token")
        
        # Create the credentials file content
        credentials = {
            "type": "authorized_user",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": updated_token_data.get("refresh_token"),
            "access_token": updated_token_data.get("access_token"),
            "token_uri": TOKEN_URL,
            # Use RFC3339 timestamp string for expiry to satisfy google-auth parsing
            "expiry": datetime.datetime.utcfromtimestamp(
                updated_token_data.get("expires_at", int(time.time()) + 3600)
            ).isoformat("T") + "Z",
            "project_id": project_id,
            "quota_project_id": project_id
        }
        
        # Create a temporary file
        fd, credentials_path = tempfile.mkstemp(suffix='.json', prefix=f'gcp_credentials_{project_id}_')
        
        # Write the credentials to the file
        with os.fdopen(fd, 'w') as file:
            json.dump(credentials, file)
        
        logger.info(f"Created credentials file at {credentials_path}")
        
        # Set environment variables for GCP tools
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        
        return credentials_path
        
    except Exception as e:
        error_msg = f"Failed to create credentials file: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)
