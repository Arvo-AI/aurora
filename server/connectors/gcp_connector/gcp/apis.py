"""
Centralized GCP API management.
Consolidates API enabling functionality from auth.py and related modules.
"""

import logging
import time
from typing import List, Dict, Optional, Tuple
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Master list of all required APIs across the Aurora platform
AURORA_REQUIRED_APIS = [
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudbilling.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "bigquery.googleapis.com",
    "sqladmin.googleapis.com",
    "appengine.googleapis.com",
    "pubsub.googleapis.com",
    "dns.googleapis.com",
    "cloudfunctions.googleapis.com",
    "firestore.googleapis.com",
    "dataflow.googleapis.com",
    "redis.googleapis.com",
    "endpoints.googleapis.com",
    "composer.googleapis.com",
    "containerregistry.googleapis.com",
    "cloudasset.googleapis.com",  # Asset Inventory for real-time feeds
]


def wait_for_operation(service, operation_name: str, timeout: int = 300) -> bool:
    """Wait for a long-running operation to complete.
    
    Args:
        service: The Google API service client
        operation_name: Name of the operation to wait for
        timeout: Maximum time to wait in seconds
        
    Returns:
        bool: True if successful, False if error or timeout
    """
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            operation = service.operations().get(name=operation_name).execute()
            
            if operation.get('done'):
                if 'error' in operation:
                    logger.error(f"Operation failed: {operation['error']}")
                    return False
                else:
                    logger.info(f"Operation completed successfully: {operation_name}")
                    return True
            
            time.sleep(2)  # Poll every 2 seconds
            
        except Exception as e:
            logger.error(f"Error checking operation status: {e}")
            return False
    
    logger.error(f"Operation timed out after {timeout} seconds")
    return False


def enable_single_api(credentials, project_id: str, api: str) -> Tuple[bool, Optional[str]]:
    """Enable a single API for a GCP project.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        api: API name (e.g., 'run.googleapis.com')
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        service_usage = build('serviceusage', 'v1', credentials=credentials)
        
        # Check if the API is already enabled
        response = service_usage.services().get(
            name=f'projects/{project_id}/services/{api}'
        ).execute()
        
        if response.get('state') == 'ENABLED':
            logger.info(f"API {api} is already enabled for project {project_id}")
            return True, None
        
        # Enable the API
        logger.info(f"Enabling API {api} for project {project_id}")
        operation = service_usage.services().enable(
            name=f'projects/{project_id}/services/{api}'
        ).execute()
        
        # Wait for operation to complete
        operation_name = operation.get('name')
        if not operation_name:
            return False, f"Failed to get operation name when enabling {api}"
        
        if wait_for_operation(service_usage, operation_name, timeout=120):
            logger.info(f"Successfully enabled {api} for project {project_id}")
            return True, None
        else:
            return False, f"Timed out or failed enabling {api}"
            
    except HttpError as e:
        if e.resp.status == 403:
            return False, f"Permission denied to enable {api}"
        return False, f"HTTP error enabling {api}: {e}"
    except Exception as e:
        return False, f"Error enabling {api}: {e}"


def enable_apis_batch(credentials, project_id: str, apis: List[str], 
                     check_project_state: bool = True) -> Tuple[bool, Optional[str]]:
    """Enable multiple APIs for a GCP project in batch.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        apis: List of API names to enable
        check_project_state: Whether to check if project is ACTIVE before enabling
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        # Check if project is ACTIVE if requested
        if check_project_state:
            crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
            try:
                proj = crm_service.projects().get(projectId=project_id).execute()
                if proj.get('lifecycleState') != 'ACTIVE':
                    logger.warning(
                        f"Project {project_id} is not ACTIVE (state: {proj.get('lifecycleState')}). "
                        "Skipping API enablement."
                    )
                    return False, f"Project not ACTIVE: {proj.get('lifecycleState')}"
            except Exception as state_err:
                logger.error(f"Failed to fetch lifecycleState for {project_id}: {state_err}")
        
        service_usage = build('serviceusage', 'v1', credentials=credentials)
        
        # Enable APIs in batches (up to 20 at a time as per Google's recommendation)
        batch_size = 20
        all_successful = True
        errors = []
        
        for i in range(0, len(apis), batch_size):
            batch = apis[i:i + batch_size]
            
            logger.info(f"Enabling {len(batch)} APIs for project {project_id} (batch {i//batch_size + 1})")
            
            try:
                # Create the batch request
                request_body = {
                    "serviceIds": batch
                }
                
                operation = service_usage.services().batchEnable(
                    parent=f"projects/{project_id}",
                    body=request_body
                ).execute()
                
                # Wait for operation to complete
                operation_name = operation.get('name')
                if operation_name:
                    if not wait_for_operation(service_usage, operation_name, timeout=600):
                        all_successful = False
                        errors.append(f"Batch {i//batch_size + 1} timed out or failed")
                    else:
                        logger.info(f"Successfully enabled batch {i//batch_size + 1} for project {project_id}")
                        
            except HttpError as e:
                logger.error(f"HTTP error enabling API batch for project {project_id}: {e}")
                all_successful = False
                errors.append(f"Batch {i//batch_size + 1}: {e}")
            except Exception as e:
                logger.error(f"Error enabling API batch for project {project_id}: {e}")
                all_successful = False
                errors.append(f"Batch {i//batch_size + 1}: {e}")
        
        if all_successful:
            return True, None
        else:
            return False, f"Failed to enable some APIs: {'; '.join(errors)}"
            
    except Exception as e:
        logger.error(f"Error enabling APIs for project {project_id}: {e}")
        return False, str(e)


def enable_apis_for_all_projects(credentials, apis: List[str] = None, projects: List[dict] = None) -> Dict[str, bool]:
    """Enable APIs for all accessible GCP projects.
    
    Args:
        credentials: Google OAuth credentials object
        apis: List of APIs to enable (defaults to AURORA_REQUIRED_APIS)
        projects: Optional list of project dicts to process. If None, fetches all projects.
        
    Returns:
        Dictionary mapping project_id to success status
    """
    if apis is None:
        apis = AURORA_REQUIRED_APIS
    
    try:
        # Get projects if not provided
        if projects is None:
            from connectors.gcp_connector.gcp.projects import get_project_list
            projects = get_project_list(credentials)
        
        if not projects:
            logger.warning("No GCP projects found")
            return {}
        
        results = {}
        for project in projects:
            # Skip non-ACTIVE projects
            if project.get('lifecycleState') != 'ACTIVE':
                logger.info(
                    f"Skipping project {project.get('projectId')} with lifecycleState "
                    f"{project.get('lifecycleState')}"
                )
                continue
            
            project_id = project.get('projectId')
            if not project_id:
                continue
            
            success, error = enable_apis_batch(credentials, project_id, apis, check_project_state=False)
            results[project_id] = success
            if not success:
                logger.error(f"Failed to enable APIs for {project_id}: {error}")
        
        return results
        
    except Exception as e:
        logger.error(f"Error enabling APIs for all projects: {e}")
        return {}


def check_api_enabled(credentials, project_id: str, api: str) -> bool:
    """Check if a specific API is enabled for a project.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        api: API name (e.g., 'run.googleapis.com')
        
    Returns:
        bool: True if enabled, False otherwise
    """
    try:
        service_usage = build('serviceusage', 'v1', credentials=credentials)
        
        response = service_usage.services().get(
            name=f'projects/{project_id}/services/{api}'
        ).execute()
        
        return response.get('state') == 'ENABLED'
        
    except Exception as e:
        logger.error(f"Error checking if {api} is enabled for {project_id}: {e}")
        return False


def list_enabled_apis(credentials, project_id: str) -> List[str]:
    """List all enabled APIs for a project.
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        
    Returns:
        List of enabled API names
    """
    try:
        service_usage = build('serviceusage', 'v1', credentials=credentials)
        
        enabled_apis = []
        page_token = None
        
        while True:
            request = service_usage.services().list(
                parent=f'projects/{project_id}',
                filter='state:ENABLED',
                pageToken=page_token
            )
            response = request.execute()
            
            services = response.get('services', [])
            for service in services:
                # Extract just the API name (e.g., 'run.googleapis.com')
                api_name = service['name'].split('/')[-1]
                enabled_apis.append(api_name)
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        
        return enabled_apis
        
    except Exception as e:
        logger.error(f"Error listing enabled APIs for {project_id}: {e}")
        return []

