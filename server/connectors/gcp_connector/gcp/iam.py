"""
IAM and permissions management for GCP projects.
"""

import logging
from typing import List, Dict
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def add_binding_if_missing(policy: dict, role: str, member: str) -> bool:
    """Add a single IAM binding if the (role, member) pair is missing.
    
    Args:
        policy: Existing IAM policy dict
        role: Role string, e.g. "roles/owner"
        member: Member string, e.g. "serviceAccount:foo@bar"
    
    Returns:
        bool: True if policy was modified, False otherwise
    """
    bindings = policy.setdefault("bindings", [])
    for b in bindings:
        if b.get("role") == role:
            if member in b.get("members", []):
                return False  # Already present
            b.setdefault("members", []).append(member)
            return True
    # role binding not present – add new
    bindings.append({"role": role, "members": [member]})
    return True


def remove_binding_member(policy: dict, role: str, member: str) -> bool:
    """Remove a member from a role binding if present.
    
    Args:
        policy: Existing IAM policy dict
        role: Role string
        member: Member string
    
    Returns:
        bool: True if policy was modified
    """
    bindings = policy.get("bindings", [])
    for b in bindings:
        if b.get("role") == role and member in b.get("members", []):
            b["members"].remove(member)
            # Remove binding entirely if no members left
            if not b["members"]:
                bindings.remove(b)
            return True
    return False


def set_project_bindings(crm_service, project_id: str, member: str, roles: List[str]):
    """Ensure member has the listed roles on the project.

    Args:
        crm_service: Cloud Resource Manager service client
        project_id: GCP project ID
        member: Member string (e.g., "user:email@example.com")
        roles: List of role names to grant
    """
    import time
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # Get fresh policy
            policy = crm_service.projects().getIamPolicy(
                resource=project_id,
                body={'options': {'requestedPolicyVersion': 3}}
            ).execute()

            # Add missing roles
            modified = False
            for role in roles:
                modified |= add_binding_if_missing(policy, role, member)

            if not modified:
                logger.info(f"No changes needed - {member} already has all roles on {project_id}")
                return

            # Apply the policy update
            logger.info(f"Granting roles to {member} on project {project_id}")
            crm_service.projects().setIamPolicy(
                resource=project_id,
                body={"policy": policy}
            ).execute()

            # Simple verification after a brief wait
            time.sleep(3)
            verify_policy = crm_service.projects().getIamPolicy(
                resource=project_id,
                body={}
            ).execute()

            # Quick check that at least some roles are present
            found_any = False
            for binding in verify_policy.get('bindings', []):
                if member in binding.get('members', []) and binding.get('role') in roles:
                    found_any = True
                    break

            if found_any:
                logger.info(f"Successfully applied roles to {member} on project {project_id}")
                return
            elif attempt < max_retries - 1:
                logger.warning(f"Roles not yet visible, retrying...")
                time.sleep(2)
                continue

        except HttpError as e:
            if e.resp.status == 409 and attempt < max_retries - 1:
                logger.warning(f"Concurrent modification, retrying...")
                time.sleep(2)
                continue
            else:
                logger.error(f"Could not update IAM policy for project {project_id}: {e}")
                raise


def remove_project_bindings(crm_service, project_id: str, member: str, roles: List[str]):
    """Ensure member no longer has the specified roles on the project.
    
    Args:
        crm_service: Cloud Resource Manager service client
        project_id: GCP project ID
        member: Member string
        roles: List of role names to remove
    """
    try:
        policy = crm_service.projects().getIamPolicy(resource=project_id, body={}).execute()
        modified = False
        for role in roles:
            modified |= remove_binding_member(policy, role, member)
        if modified:
            crm_service.projects().setIamPolicy(resource=project_id, body={"policy": policy}).execute()
            logger.info(f"Removed roles {roles} for {member} on {project_id}")
    except HttpError as e:
        logger.warning(f"Could not update IAM policy for project {project_id}: {e}")


def set_org_bindings(crm_service, org_id: str, member: str, roles: List[str]):
    """Ensure member has roles on the organisation if caller has permissions.
    
    Args:
        crm_service: Cloud Resource Manager service client
        org_id: Organization ID
        member: Member string
        roles: List of role names to grant
    """
    try:
        name = org_id if org_id.startswith("organizations/") else f"organizations/{org_id}"
        policy = crm_service.organizations().getIamPolicy(resource=name, body={}).execute()
        modified = False
        for role in roles:
            modified |= add_binding_if_missing(policy, role, member)
        if modified:
            crm_service.organizations().setIamPolicy(resource=name, body={"policy": policy}).execute()
            logger.info(f"Updated IAM policy for org {org_id} – added roles {roles} for {member}")
    except HttpError as e:
        logger.info(f"No permission to set org-level IAM policy ({org_id}): {e}")


def set_service_account_policy(iam_service, sa_resource_name: str, user_member: str):
    """Add TokenCreator binding on the service account for the user.
    
    Args:
        iam_service: IAM service client
        sa_resource_name: Full service account resource name
        user_member: User member string (e.g., "user:email@example.com")
    """
    try:
        policy = iam_service.projects().serviceAccounts().getIamPolicy(resource=sa_resource_name).execute()
        modified = add_binding_if_missing(policy, "roles/iam.serviceAccountTokenCreator", user_member)
        if modified:
            iam_service.projects().serviceAccounts().setIamPolicy(
                resource=sa_resource_name, 
                body={"policy": policy}
            ).execute()
            logger.info(f"Added impersonation (TokenCreator) binding on {sa_resource_name} for {user_member}")
    except HttpError as e:
        logger.warning(f"Failed to set policy on service account {sa_resource_name}: {e}")
        raise


def allow_public_access_iam_policy(credentials, project_id: str) -> bool:
    """Check if public IAM principals (allUsers) are allowed, and enable if not.
    
    This checks if listPolicy.allValues = "ALLOW" is set for
    constraints/iam.allowedPolicyMemberDomains at the project level, which is
    required to grant the Cloud Run Invoker role to "allUsers".
    
    Args:
        credentials: Google OAuth credentials object
        project_id: GCP project ID
        
    Returns:
        bool: True if the policy allows public access (either already set or successfully set)
    """
    try:
        from googleapiclient.discovery import build
        
        crm_service = build('cloudresourcemanager', 'v1', credentials=credentials)
        resource_name = f"projects/{project_id}"
        
        # First, check if the policy is already correctly configured
        try:
            current_policy = crm_service.projects().getOrgPolicy(
                resource=resource_name,
                body={"constraint": "constraints/iam.allowedPolicyMemberDomains"}
            ).execute()
            
            # Check if policy already allows all values
            list_policy = current_policy.get('listPolicy', {})
            if list_policy.get('allValues') == 'ALLOW':
                logger.info(f"Org Policy already allows public access for project {project_id}")
                return True
                
        except HttpError as check_err:
            # Policy might not exist yet, which is fine - we'll try to create it
            if check_err.resp.status != 404:
                logger.warning(f"Could not check existing policy for {project_id}: {check_err}")
        
        # Policy is not set correctly, try to set it
        def _set_policy():
            policy_body = {
                "policy": {
                    "constraint": "constraints/iam.allowedPolicyMemberDomains",
                    "listPolicy": {
                        "allValues": "ALLOW"
                    }
                }
            }
            crm_service.projects().setOrgPolicy(resource=resource_name, body=policy_body).execute()
        
        try:
            _set_policy()
            logger.info(f"Successfully set Org Policy to allow public IAM principals for project {project_id}")
            return True
        except HttpError as err:
            if err.resp.status == 403 or 'PERMISSION_DENIED' in str(err):
                logger.warning(f"Cannot set Org Policy for {project_id} due to insufficient permissions")
                return False
            else:
                logger.error(f"Failed to set Org Policy for project {project_id}: {err}")
                return False
    
    except Exception as e:
        logger.error(f"Unexpected error checking/setting Org Policy for project {project_id}: {e}")
        return False


def allow_public_access_for_all_projects(credentials, projects=None) -> Dict[str, bool]:
    """Iterate over all accessible projects and relax Org Policy to allow public IAM principals.
    
    Args:
        credentials: Google OAuth credentials object
        projects: Optional list of project dicts to process. If None, fetches all projects.
        
    Returns:
        Dictionary mapping project_id to success status
    """
    try:
        # Get projects if not provided
        if projects is None:
            from connectors.gcp_connector.gcp.projects import get_project_list
            projects = get_project_list(credentials)
        
        if not projects:
            logger.warning("No GCP projects found while attempting to update Org Policy")
            return {}
        
        results = {}
        for proj in projects:
            pid = proj.get("projectId")
            if not pid:
                continue
            success = allow_public_access_iam_policy(credentials, pid)
            results[pid] = success
        return results
    except Exception as e:
        logger.error(f"Error updating Org Policy for projects: {e}")
        return {}

