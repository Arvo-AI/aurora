"""
GCP project operations for user's own Google Cloud Platform projects.
"""

import logging
from typing import List, Dict, Optional
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def get_project_list(credentials) -> List[Dict]:
    """Fetch all GCP projects accessible to the authenticated user.
    
    Args:
        credentials: Google OAuth credentials object
        
    Returns:
        List of project dictionaries
    """
    try:
        service = build('cloudresourcemanager', 'v1', credentials=credentials)
        project_list = service.projects().list().execute()
        return project_list.get('projects', [])
    except Exception as e:
        logger.error(f"Failed to fetch project list: {e}")
        raise ValueError(f"Failed to fetch project list: {e}")


def list_gke_clusters(credentials, project_id: str, location: str = "-") -> List[Dict]:
    """List all GKE clusters in the specified project and location.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        location: GCP location (use "-" for all locations)
        
    Returns:
        List of cluster dictionaries
    """
    try:
        service = build('container', 'v1', credentials=credentials)
        response = service.projects().locations().clusters().list(
            parent=f"projects/{project_id}/locations/{location}"
        ).execute()
        return response.get('clusters', [])
    except Exception as e:
        logger.error(f"Failed to list GKE clusters: {e}")
        raise ValueError(f"Failed to list GKE clusters: {e}")


def check_billing_enabled(credentials, project_id: str) -> bool:
    """Return True if Cloud Billing is enabled for project_id.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        
    Returns:
        bool: True if billing is enabled, False otherwise
    """
    try:
        billing_service = build("cloudbilling", "v1", credentials=credentials)
        billing_info = billing_service.projects().getBillingInfo(
            name=f"projects/{project_id}"
        ).execute()
        return billing_info.get("billingEnabled", False)
    except Exception as e:
        logger.debug(f"Could not check billing status for project {project_id}: {e}")
        return False


def select_best_project(credentials, projects: List[Dict], user_id: str = None) -> str:
    """Select the best project based on priority: user preference > billing enabled > first.

    Args:
        credentials: Google OAuth credentials object
        projects: List of project dictionaries
        user_id: Optional user ID to check for stored preference

    Returns:
        str: Selected project ID
    """
    if not projects:
        raise ValueError("No projects available")

    project_ids = [p.get('projectId') for p in projects if p.get('projectId')]

    # First, check if user has a stored root project preference
    if user_id:
        try:
            from utils.auth.stateless_auth import get_user_preference
            root_project_pref = get_user_preference(user_id, 'gcp_root_project')

            if root_project_pref and root_project_pref in project_ids:
                # Validate the preferred project still has billing and IAM access
                if check_billing_enabled(credentials, root_project_pref):
                    try:
                        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
                        crm_service.projects().getIamPolicy(resource=root_project_pref, body={}).execute()
                        logger.info(f"Using user-preferred root project: {root_project_pref}")
                        return root_project_pref
                    except Exception as e:
                        logger.warning(f"User-preferred project {root_project_pref} no longer valid: {e}")
        except Exception as e:
            logger.debug(f"Could not check user preference: {e}")

    # Fall back to automatic selection
    # Try to find a project with billing enabled AND IAM permissions
    crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
    for project in projects:
        project_id = project.get('projectId')
        if not project_id:
            continue
        # Check both billing and IAM permissions
        try:
            if check_billing_enabled(credentials, project_id):
                # Test IAM permission by trying to read policy
                crm_service.projects().getIamPolicy(resource=project_id, body={}).execute()
                logger.info(f"Selected billing-enabled project with IAM access: {project_id}")
                return project_id
        except Exception as e:
            logger.warning(f"Skipping project {project_id}: {e}")
            continue

    # Fallback to first project
    project_id = projects[0].get('projectId')
    logger.info(f"Using first project as fallback: {project_id}")
    return project_id


def get_project_details(credentials, project_id: str) -> Optional[Dict]:
    """Get detailed information about a specific project.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        
    Returns:
        Project details dictionary or None if not found
    """
    try:
        service = build('cloudresourcemanager', 'v1', credentials=credentials)
        project = service.projects().get(projectId=project_id).execute()
        return project
    except HttpError as e:
        if e.resp.status == 404:
            logger.warning(f"Project {project_id} not found")
            return None
        logger.error(f"Error getting project {project_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error getting project {project_id}: {e}")
        return None


def list_projects_with_filter(credentials, filter_str: str) -> List[Dict]:
    """List projects with a specific filter.
    
    Args:
        credentials: Google OAuth credentials object
        filter_str: Filter string (e.g., "labels.aurora-managed:true")
        
    Returns:
        List of project dictionaries
    """
    try:
        service = build('cloudresourcemanager', 'v1', credentials=credentials)
        response = service.projects().list(filter=filter_str).execute()
        return response.get('projects', [])
    except Exception as e:
        logger.error(f"Failed to list projects with filter '{filter_str}': {e}")
        return []


def get_organization_id(credentials) -> Optional[str]:
    """Return first organization ID accessible, else None.
    
    Args:
        credentials: Google OAuth credentials object
        
    Returns:
        Organization ID string or None
    """
    try:
        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
        resp = crm_service.organizations().search(body={}).execute()
        orgs = resp.get("organizations", [])
        if orgs:
            return orgs[0]["name"]  # returns like "organizations/123456"
    except Exception as e:
        logger.debug(f"Could not get organization ID: {e}")
    return None

