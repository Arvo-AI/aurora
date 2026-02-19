"""
Scaleway authentication and credential validation.

Handles:
- Validating Scaleway API credentials
- Fetching user/organization info
- Listing projects
"""

import logging
import requests
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SCALEWAY_API_BASE = "https://api.scaleway.com"


def validate_scaleway_credentials(
    access_key: str,
    secret_key: str,
    organization_id: Optional[str] = None,
    project_id: Optional[str] = None
) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate Scaleway API credentials by fetching account info.
    
    Args:
        access_key: Scaleway access key (SCW_ACCESS_KEY)
        secret_key: Scaleway secret key (SCW_SECRET_KEY)
        organization_id: Optional organization ID
        project_id: Optional default project ID
        
    Returns:
        Tuple of (success, account_info, error_message)
    """
    if not access_key or not secret_key:
        return False, None, "Access key and secret key are required"
    
    headers = {
        "X-Auth-Token": secret_key,
        "Content-Type": "application/json"
    }
    
    try:
        # First, get organization ID from IAM API if not provided
        if not organization_id:
            # Get current API key info to find the organization
            iam_response = requests.get(
                f"{SCALEWAY_API_BASE}/iam/v1alpha1/api-keys/{access_key}",
                headers=headers,
                timeout=10
            )
            
            if iam_response.status_code == 401:
                return False, None, "Invalid credentials. Please check your access key and secret key."
            
            if iam_response.status_code == 403:
                return False, None, "Access denied. Please check your API key permissions."
            
            if not iam_response.ok:
                return False, None, f"Failed to validate API key: {iam_response.status_code}"
            
            api_key_info = iam_response.json()
            # Get default_project_id from API key info (for later use)
            if not project_id:
                project_id = api_key_info.get("default_project_id")
            
            # Get organization_id from the API key's associated user or application
            # Note: organization_id is NOT in api_key_info, must fetch from user/application
            user_id = api_key_info.get("user_id")
            application_id = api_key_info.get("application_id")
            
            if user_id:
                user_response = requests.get(
                    f"{SCALEWAY_API_BASE}/iam/v1alpha1/users/{user_id}",
                    headers=headers,
                    timeout=10
                )
                if user_response.ok:
                    user_info = user_response.json()
                    organization_id = user_info.get("organization_id")
            elif application_id:
                # For service account API keys, get org from application
                app_response = requests.get(
                    f"{SCALEWAY_API_BASE}/iam/v1alpha1/applications/{application_id}",
                    headers=headers,
                    timeout=10
                )
                if app_response.ok:
                    app_info = app_response.json()
                    organization_id = app_info.get("organization_id")
        
        # Fetch projects list - organization_id is required
        params = {"organization_id": organization_id} if organization_id else {}
        response = requests.get(
            f"{SCALEWAY_API_BASE}/account/v3/projects",
            headers=headers,
            params=params,
            timeout=10
        )
        
        if response.status_code == 401:
            return False, None, "Invalid credentials. Please check your access key and secret key."
        
        if response.status_code == 403:
            return False, None, "Access denied. Please check your API key permissions."
        
        if not response.ok:
            return False, None, f"API error: {response.status_code} - {response.text}"
        
        projects_data = response.json()
        projects = projects_data.get("projects", [])
        
        # Get organization info from first project if not provided
        if projects and not organization_id:
            organization_id = projects[0].get("organization_id")
        
        # If project_id not specified, use first project
        if projects and not project_id:
            project_id = projects[0].get("id")
        
        account_info = {
            "access_key": access_key,
            "organization_id": organization_id,
            "default_project_id": project_id,
            "projects": projects,
            "projects_count": len(projects)
        }
        
        logger.info(f"Scaleway credentials validated. Found {len(projects)} projects.")
        return True, account_info, None
        
    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"Scaleway API request failed: {e}")
        return False, None, "Connection error. Please check your network and try again."
    except Exception as e:
        logger.error(f"Unexpected error validating Scaleway credentials: {e}")
        return False, None, "An unexpected error occurred. Please try again."


def get_scaleway_projects(secret_key: str, organization_id: Optional[str] = None, access_key: Optional[str] = None) -> Tuple[bool, list, Optional[str]]:
    """
    Fetch all Scaleway projects accessible with the given credentials.
    
    Args:
        secret_key: Scaleway secret key
        organization_id: Optional organization ID (will be fetched if not provided)
        access_key: Optional access key (needed to fetch organization_id)
        
    Returns:
        Tuple of (success, projects_list, error_message)
    """
    headers = {
        "X-Auth-Token": secret_key,
        "Content-Type": "application/json"
    }
    
    try:
        # Get organization_id if not provided
        if not organization_id and access_key:
            iam_response = requests.get(
                f"{SCALEWAY_API_BASE}/iam/v1alpha1/api-keys/{access_key}",
                headers=headers,
                timeout=10
            )
            if iam_response.ok:
                api_key_info = iam_response.json()
                # Try to get organization_id from user info
                user_id = api_key_info.get("user_id")
                if user_id:
                    user_response = requests.get(
                        f"{SCALEWAY_API_BASE}/iam/v1alpha1/users/{user_id}",
                        headers=headers,
                        timeout=10
                    )
                    if user_response.ok:
                        user_info = user_response.json()
                        organization_id = user_info.get("organization_id")
        
        # Fetch projects with organization_id
        params = {"organization_id": organization_id} if organization_id else {}
        response = requests.get(
            f"{SCALEWAY_API_BASE}/account/v3/projects",
            headers=headers,
            params=params,
            timeout=10
        )
        
        if response.status_code == 401:
            return False, [], "Invalid or expired credentials"
        
        if not response.ok:
            return False, [], f"API error: {response.status_code}"
        
        projects_data = response.json()
        projects = projects_data.get("projects", [])
        
        # Format projects for frontend
        formatted_projects = []
        for project in projects:
            formatted_projects.append({
                "projectId": project.get("id"),
                "projectName": project.get("name", project.get("id")),
                "organizationId": project.get("organization_id"),
                "description": project.get("description", ""),
                "enabled": True
            })
        
        return True, formatted_projects, None
        
    except Exception as e:
        logger.error(f"Failed to fetch Scaleway projects: {e}")
        return False, [], "Failed to fetch projects"


def get_account_info(secret_key: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Get Scaleway account/organization info.
    
    Args:
        secret_key: Scaleway secret key
        
    Returns:
        Tuple of (success, account_info, error_message)
    """
    headers = {
        "X-Auth-Token": secret_key,
        "Content-Type": "application/json"
    }
    
    try:
        # Get organization info
        response = requests.get(
            f"{SCALEWAY_API_BASE}/account/v3/projects",
            headers=headers,
            timeout=10
        )
        
        if not response.ok:
            return False, None, f"API error: {response.status_code}"
        
        projects_data = response.json()
        projects = projects_data.get("projects", [])
        
        if not projects:
            return False, None, "No projects found"
        
        # Extract organization from first project
        organization_id = projects[0].get("organization_id")
        
        account_info = {
            "organization_id": organization_id,
            "projects_count": len(projects),
            "default_project_id": projects[0].get("id"),
            "default_project_name": projects[0].get("name")
        }
        
        return True, account_info, None
        
    except Exception as e:
        logger.error(f"Failed to get Scaleway account info: {e}")
        return False, None, "Failed to get account info"
