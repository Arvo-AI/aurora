"""
OVH API Routes - Projects and Onboarding

This module provides endpoints for:
1. Fetching OVH cloud projects
2. Validating IAM permissions
3. Granting IAM permissions (auto-grant feature)
4. Connection status and disconnect
"""
import logging
import json
import requests
from flask import request, jsonify

from routes.ovh import ovh_bp
from routes.ovh.oauth2_auth_code_flow import get_valid_access_token
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    store_user_preference,
    get_user_preference,
)
from utils.web.limiter_ext import limiter
from config.rate_limiting import OVH_READ_LIMITS
from utils.secrets.secret_ref_utils import has_user_credentials, delete_user_secret
from utils.db.connection_utils import set_connection_status
from utils.web.cors_utils import create_cors_response
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.ssh.ssh_utils import (
    delete_ssh_credentials,
    load_user_private_key_safe,
    normalize_private_key,
    parse_ssh_key_id,
    validate_private_key_format,
    validate_and_test_ssh
)

logger = logging.getLogger(__name__)

# OVH API Endpoints
OVH_API_ENDPOINTS = {
    'ovh-eu': 'https://eu.api.ovh.com/1.0',
    'ovh-us': 'https://api.us.ovhcloud.com/1.0',
    'ovh-ca': 'https://ca.api.ovh.com/1.0',
}


@ovh_bp.route('/ovh/projects', methods=['GET', 'POST', 'OPTIONS'])
@limiter.limit(OVH_READ_LIMITS)
def ovh_projects():
    """
    GET /ovh_api/ovh/projects - Fetch list of OVH cloud projects
    POST /ovh_api/ovh/projects - Save project enabled/disabled preferences

    GET Returns:
    {
        "projects": [
            {"projectId": "abc123", "projectName": "Production", "status": "ok", "enabled": true},
        ]
    }
    
    POST Body:
    {
        "projects": [{"projectId": "abc123", "enabled": false}]
    }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    # Handle POST - save project preferences
    if request.method == 'POST':
        try:
            user_id = get_user_id_from_request()
            if not user_id:
                return jsonify({"error": "Missing user_id"}), 401
            
            data = request.get_json() or {}
            projects = data.get('projects', [])
            
            if not projects:
                return jsonify({"error": "No projects provided"}), 400
            
            # Store project preferences in user preferences (database)
            
            # Get existing preferences or create new
            existing_prefs = get_user_preference(user_id, 'ovh_project_preferences') or {}
            
            # Update with new project settings
            for project in projects:
                project_id = project.get('projectId')
                enabled = project.get('enabled', True)
                if project_id:
                    existing_prefs[project_id] = {'enabled': enabled}
            
            # Save updated preferences
            store_user_preference(user_id, 'ovh_project_preferences', existing_prefs)
            
            logger.info(f"Saved OVH project preferences for user {user_id}: {len(projects)} projects")
            return jsonify({"success": True, "message": "Project preferences saved"})
            
        except Exception as e:
            logger.error(f"Error saving OVH project preferences: {e}", exc_info=True)
            return jsonify({"error": "Failed to save project preferences"}), 500
    
    try:
        # Get user ID from request
        user_id = get_user_id_from_request()
        if not user_id:
            logger.warning("Projects fetch attempt without user_id")
            return jsonify({"error": "Missing user_id"}), 401

        logger.info(f"Fetching OVH projects for user: {user_id} (from header: {request.headers.get('X-User-ID')})")

        # Get valid token data (auto-refreshes if expired)
        token_data = get_valid_access_token(user_id)
        if not token_data:
            logger.warning(f"Failed to get valid OVH token for user: {user_id}")
            return jsonify({
                "error": "OVH authentication expired. Please reconnect your account.",
                "action": "RECONNECT_REQUIRED"
            }), 401

        # Extract access token and endpoint from token data
        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint')

        if not access_token or not endpoint:
            logger.error(f"Invalid token data for user: {user_id}")
            return jsonify({"error": "Invalid OVH configuration"}), 400

        # Get API base URL for the endpoint
        api_base_url = OVH_API_ENDPOINTS.get(endpoint)
        if not api_base_url:
            logger.error(f"Invalid OVH endpoint: {endpoint}")
            return jsonify({"error": "Invalid OVH endpoint"}), 400

        # Call OVH API to get projects
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = requests.get(
            f'{api_base_url}/cloud/project',
            headers=headers,
            timeout=10
        )

        if response.status_code == 401:
            logger.warning(f"OVH access token expired or invalid for user: {user_id}")
            return jsonify({"error": "OVH authentication expired. Please reconnect your OVH account."}), 401

        if not response.ok:
            logger.error(f"OVH API error: {response.status_code} - {response.text}")
            return jsonify({"error": "OVH API request failed"}), response.status_code

        # Parse project IDs
        project_ids = response.json()

        # Fetch details for each project
        projects = []
        for project_id in project_ids:
            try:
                project_response = requests.get(
                    f'{api_base_url}/cloud/project/{project_id}',
                    headers=headers,
                    timeout=5
                )

                if project_response.ok:
                    project_data = project_response.json()
                    projects.append({
                        'projectId': project_id,
                        'projectName': project_data.get('description', project_id),
                        'status': project_data.get('status', 'unknown'),
                        'enabled': True
                    })
                else:
                    # If we can't get details, still include the project ID
                    projects.append({
                        'projectId': project_id,
                        'projectName': project_id,
                        'status': 'unknown',
                        'enabled': True
                    })
            except Exception as e:
                logger.warning(f"Failed to fetch details for project {project_id}: {e}")
                projects.append({
                    'projectId': project_id,
                    'projectName': project_id,
                    'status': 'unknown',
                    'enabled': True
                })

        # Merge with saved project preferences and get root project
        root_project = None
        try:
            saved_prefs = get_user_preference(user_id, 'ovh_project_preferences') or {}
            root_project = get_user_preference(user_id, 'ovh_root_project')
            
            logger.info(f"OVH root_project preference for user {user_id}: {root_project} (type: {type(root_project)})")
            
            for project in projects:
                project_id = project.get('projectId')
                if project_id:
                    # Apply enabled preference
                    if project_id in saved_prefs:
                        project['enabled'] = saved_prefs[project_id].get('enabled', True)
                    # Mark root project
                    is_root = project_id == root_project
                    logger.info(f"Comparing project_id={project_id} with root_project={root_project}: {is_root}")
                    project['isRootProject'] = is_root
        except Exception as e:
            logger.warning(f"Failed to load project preferences: {e}", exc_info=True)
        
        logger.info(f"Successfully fetched {len(projects)} projects for user: {user_id}")
        return jsonify({'projects': projects, 'root_project': root_project})

    except Exception as e:
        logger.error(f"Error fetching OVH projects: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch OVH projects"}), 500


@ovh_bp.route('/ovh/instances', methods=['GET', 'OPTIONS'])
@limiter.limit(OVH_READ_LIMITS)
def ovh_instances():
    """
    GET /ovh_api/ovh/instances - Fetch all OVH instances across user's projects
    
    Returns:
    {
        "instances": [
            {
                "id": "instance-id",
                "name": "vm-name",
                "status": "ACTIVE",
                "ipAddresses": [{"ip": "1.2.3.4", "type": "public"}],
                "region": "GRA7",
                "sshConfig": null
            }
        ]
    }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        logger.info(f"Fetching OVH instances for user: {user_id}")
        
        # Get valid token data
        token_data = get_valid_access_token(user_id)
        if not token_data:
            return jsonify({"error": "Not authenticated"}), 401
        
        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint')
        api_base_url = OVH_API_ENDPOINTS.get(endpoint)
        
        if not access_token or not api_base_url:
            return jsonify({"error": "Invalid configuration"}), 400
        
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        # Get projects
        projects_response = requests.get(
            f'{api_base_url}/cloud/project',
            headers=headers,
            timeout=10
        )
        
        if not projects_response.ok:
            logger.error(f"Failed to fetch projects: {projects_response.status_code}")
            return jsonify({"error": "Failed to fetch projects"}), 500
        
        project_ids = projects_response.json()
        all_instances = []
        
        # Fetch instances from each project
        for project_id in project_ids:
            try:
                instances_response = requests.get(
                    f'{api_base_url}/cloud/project/{project_id}/instance',
                    headers=headers,
                    timeout=5
                )
                
                if instances_response.ok:
                    instances = instances_response.json()
                    for instance in instances:
                        # Handle image as dict with 'name' or as string ID
                        image_obj = instance.get('image')
                        if isinstance(image_obj, dict):
                            image_name = image_obj.get('name', '')
                        else:
                            # If image is string or missing, fetch full instance details
                            instance_id = instance.get('id')
                            try:
                                detail_resp = requests.get(
                                    f'{api_base_url}/cloud/project/{project_id}/instance/{instance_id}',
                                    headers=headers,
                                    timeout=3
                                )
                                if detail_resp.ok:
                                    detail_instance = detail_resp.json()
                                    image_name = detail_instance.get('image', {}).get('name', '')
                                else:
                                    image_name = ''
                            except:
                                image_name = ''
                        
                        instance_id = instance.get('id')
                        
                        # Check if SSH key exists for this instance
                        ssh_configured = False
                        try:
                            ssh_token_data = get_token_data(user_id, f"ovh_ssh_{instance_id}")
                            ssh_configured = bool(ssh_token_data and ssh_token_data.get('private_key'))
                        except Exception as e:
                            logger.debug(f"No SSH key found for instance {instance_id}: {e}")
                        
                        all_instances.append({
                            'id': instance_id,
                            'name': instance.get('name'),
                            'status': instance.get('status'),
                            'ipAddresses': instance.get('ipAddresses', []),
                            'region': instance.get('region'),
                            'projectId': project_id,
                            'imageName': image_name,
                            'sshConfig': ssh_configured
                        })
                else:
                    logger.warning(f"Failed to fetch instances from project {project_id}: {instances_response.status_code}")
            except Exception as e:
                logger.warning(f"Error fetching instances from project {project_id}: {e}")
                continue
        
        logger.info(f"Successfully fetched {len(all_instances)} OVH instances for user {user_id}")
        return jsonify({'instances': all_instances})
    
    except Exception as e:
        logger.error(f"Error fetching OVH instances: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch OVH instances"}), 500


@ovh_bp.route('/ovh/instances/<instance_id>/ssh-keys', methods=['POST', 'DELETE'])
@limiter.limit(OVH_READ_LIMITS)
def save_ovh_ssh_keys(instance_id):
    """
    Save SSH private key for an OVH instance
    
    POST /ovh_api/ovh/instances/<instance_id>/ssh-keys
    
    Request body:
    {
        "privateKey": "-----BEGIN OPENSSH PRIVATE KEY-----\\n..."
    }
    
    Returns:
    {
        "success": true,
        "message": "SSH key saved successfully"
    }
    """
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 401
    
    # Handle DELETE request
    if request.method == 'DELETE':
        success, status_code, message = delete_ssh_credentials(user_id, instance_id, 'ovh')
        return jsonify({"success": success, "message": message} if success else {"error": message}), status_code
    
    # Handle POST request
    logger.info(f"Saving SSH key for OVH instance {instance_id}, user: {user_id}")
    
    data = request.get_json() or {}
    private_key = data.get("privateKey")
    ssh_key_id = data.get("sshKeyId") or data.get("ssh_key_id")
    custom_username = data.get('username', '').strip()  # Optional custom username

    if ssh_key_id is None and not private_key:
        return jsonify({"error": "Missing sshKeyId or privateKey in request body"}), 400

    if ssh_key_id is not None:
        ssh_key_id, error_msg = parse_ssh_key_id(ssh_key_id)
        if error_msg:
            return jsonify({"error": error_msg}), 400
        private_key, error_msg = load_user_private_key_safe(user_id, ssh_key_id)
        if error_msg:
            return jsonify({"error": error_msg}), 400
    else:
        # Normalize and validate provided private key format
        try:
            private_key = normalize_private_key(private_key)  # type: ignore[arg-type]
            is_valid, error_msg = validate_private_key_format(private_key)
            if not is_valid:
                return jsonify({"error": error_msg}), 400
        except ValueError as e:
            return jsonify({"error": "Invalid private key format"}), 400
    
    logger.info(f"Received private key for instance {instance_id}, length: {len(private_key)} chars")
    
    # Fetch instance details and test SSH connection
    
    token_data = get_valid_access_token(user_id)
    if not token_data:
        return jsonify({"error": "Not authenticated with OVH"}), 401
    
    access_token = token_data.get('access_token')
    endpoint = token_data.get('endpoint')
    api_base_url = OVH_API_ENDPOINTS.get(endpoint)
    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    
    logger.info(f"Starting SSH validation for instance {instance_id}")
    
    # Find instance IP and determine SSH username
    instance_ip = None
    ssh_username = 'debian'  # Default fallback
    
    projects_resp = requests.get(f'{api_base_url}/cloud/project', headers=headers, timeout=10)
    if not projects_resp.ok:
        logger.error(f"Failed to fetch projects: {projects_resp.status_code}")
        return jsonify({"error": "Failed to fetch OVH projects"}), 500
    
    for project_id in projects_resp.json():
        try:
            inst_resp = requests.get(
                f'{api_base_url}/cloud/project/{project_id}/instance/{instance_id}', 
                headers=headers, 
                timeout=5
            )
            if inst_resp.ok:
                instance = inst_resp.json()
                instance_ip = instance.get('ipAddresses', [{}])[0].get('ip')
                image_name = instance.get('image', {}).get('name', '').lower()
                state = instance.get('status', 'unknown')
                
                logger.info(f"Found instance in project {project_id}, IP: {instance_ip}, state: {state}")
                
                if not instance_ip:
                    return jsonify({"error": "Instance has no IP address"}), 400
                
                if state != 'ACTIVE':
                    return jsonify({"error": f"Instance is not active (state: {state}). Please start the instance and try again."}), 400
                
                # Determine SSH username
                if custom_username:
                    ssh_username = custom_username
                    logger.info(f"Using custom SSH username: {ssh_username}")
                else:
                    # Auto-detect from image name
                    os_username_map = {
                        'ubuntu': 'ubuntu', 'debian': 'debian', 'centos': 'centos', 'fedora': 'fedora',
                        'alpine': 'alpine', 'arch': 'arch', 'rhel': 'ec2-user', 'rocky': 'rocky', 
                        'almalinux': 'almalinux', 'cloudlinux': 'cloudlinux'
                    }
                    username_detected = False
                    for os_name, username in os_username_map.items():
                        if os_name in image_name:
                            ssh_username = username
                            username_detected = True
                            break
                    
                    if username_detected:
                        logger.info(f"Detected SSH username: {ssh_username} (from image: {image_name})")
                    else:
                        logger.warning(f"Could not detect OS from image name '{image_name}', using default username '{ssh_username}'. SSH may fail if the image uses a different username.")
                break
        except Exception as e:
            logger.debug(f"Project {project_id} check: {e}")
            continue
    
    if not instance_ip:
        return jsonify({"error": "Instance not found or has no IP address"}), 404
    
    # Test SSH connection using shared utility
    # Wrap in try/catch to handle race condition where VM could shut down between state check and SSH attempt
    try:
        success, error_msg, connected_as = validate_and_test_ssh(instance_ip, ssh_username, private_key, timeout=30)
        
        if not success:
            return jsonify({"error": error_msg}), 400
    except Exception as e:
        logger.error(f"SSH validation failed unexpectedly: {e}", exc_info=True)
        return jsonify({"error": "SSH validation failed. The instance may have become unavailable."}), 400
    
    # Store SSH key only after successful validation
    ssh_data = {
        "private_key": private_key,
        "instance_id": instance_id,
        "validated": True,
        "saved_at": json.dumps({"timestamp": "now"})
    }
    
    store_tokens_in_db(user_id, ssh_data, f"ovh_ssh_{instance_id}")
    
    logger.info(f"Successfully saved SSH key for instance {instance_id}, user {user_id}")
    return jsonify({
        "success": True,
        "message": f"SSH key validated and saved successfully (connected as {connected_as})"
    })


@ovh_bp.route('/ovh/root-project', methods=['GET', 'POST', 'OPTIONS'])
@limiter.limit(OVH_READ_LIMITS)
def ovh_root_project():
    """
    GET /ovh_api/ovh/root-project - Get current root project
    POST /ovh_api/ovh/root-project - Set root project
    
    POST Body: { "projectId": "abc123" }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        if request.method == 'GET':
            root_project = get_user_preference(user_id, 'ovh_root_project')
            return jsonify({"root_project": root_project})
        
        elif request.method == 'POST':
            data = request.get_json() or {}
            project_id = data.get('projectId')
            
            if not project_id:
                return jsonify({"error": "projectId is required"}), 400
            
            # Store the root project preference
            logger.info(f"Storing OVH root project preference: user={user_id} (from header: {request.headers.get('X-User-ID')}), project={project_id}")
            store_user_preference(user_id, 'ovh_root_project', project_id)
            
            # Verify it was stored
            stored_value = get_user_preference(user_id, 'ovh_root_project')
            logger.info(f"Verified stored OVH root project: {stored_value} (type: {type(stored_value)})")
            
            if stored_value != project_id:
                logger.error(f"Root project not stored correctly! Expected {project_id}, got {stored_value}")
                return jsonify({"error": "Failed to store root project preference"}), 500
            
            logger.info(f"Set OVH root project to {project_id} for user {user_id}")
            return jsonify({
                "success": True,
                "root_project": project_id,
                "message": "Root project set successfully"
            })
            
    except Exception as e:
        logger.error(f"Error handling OVH root project: {e}", exc_info=True)
        return jsonify({"error": "Failed to handle root project request"}), 500


@ovh_bp.route('/ovh/onboarding/validate', methods=['GET'])
@limiter.limit(OVH_READ_LIMITS)
def ovh_validate_access():
    """
    GET /ovh_api/ovh/onboarding/validate

    Validate if the user has IAM permissions to access OVH project resources.
    Uses automatic token refresh if needed.

    Returns:
    {
        "permission": "ok" | "not_ok",
        "projectId": "abc123",
        "clientId": "xyz789",
        "message": "description of permission status"
    }
    """
    try:
        # Get user ID from request
        user_id = get_user_id_from_request()
        if not user_id:
            logger.warning("Permission validation attempt without user_id")
            return jsonify({"error": "Missing user_id"}), 401

        logger.info(f"Validating OVH access for user: {user_id}")

        # Get valid token data (auto-refreshes if expired)
        token_data = get_valid_access_token(user_id)
        if not token_data:
            logger.warning(f"No valid OVH token for user: {user_id}")
            return jsonify({
                "permission": "not_ok",
                "message": "Not authenticated with OVH"
            }), 200

        # Extract data from token
        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint')
        client_id = token_data.get('client_id', 'unknown')

        if not access_token or not endpoint:
            return jsonify({
                "permission": "not_ok",
                "message": "Invalid OVH configuration"
            }), 200

        # Get API base URL
        api_base_url = OVH_API_ENDPOINTS.get(endpoint)
        if not api_base_url:
            return jsonify({"error": "Invalid OVH endpoint"}), 400

        # Try to fetch projects to validate permissions
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = requests.get(
            f'{api_base_url}/cloud/project',
            headers=headers,
            timeout=10
        )

        if response.ok:
            # User has access
            project_ids = response.json()
            project_id = project_ids[0] if project_ids else None

            return jsonify({
                "permission": "ok",
                "projectId": project_id,
                "clientId": client_id,
                "message": "Access validated successfully"
            })
        elif response.status_code == 403:
            # User doesn't have IAM permissions
            return jsonify({
                "permission": "not_ok",
                "clientId": client_id,
                "message": "IAM permissions required"
            })
        else:
            # Other error (token expired, etc.)
            return jsonify({
                "permission": "not_ok",
                "clientId": client_id,
                "message": f"Validation failed: {response.status_code}"
            })

    except Exception as e:
        logger.error(f"Error validating OVH access: {e}", exc_info=True)
        return jsonify({"error": "Failed to validate OVH access"}), 500


@ovh_bp.route('/ovh/onboarding/grant-access', methods=['POST'])
@limiter.limit("5 per minute;20 per hour")
def ovh_grant_access():
    """
    POST /ovh_api/ovh/onboarding/grant-access

    Attempt to automatically grant IAM permissions for OVH project access.
    Uses automatic token refresh if needed.

    Request body:
    {
        "projectId": "abc123"
    }

    Returns:
    Success (200):
    {
        "success": true,
        "message": "IAM permissions granted successfully"
    }

    Manual setup required (403):
    {
        "success": false,
        "manualSteps": [
            "Step 1: Go to OVH IAM console",
            "Step 2: Create new policy",
            "Step 3: Grant permissions"
        ]
    }
    """
    try:
        # Get user ID from request
        user_id = get_user_id_from_request()
        if not user_id:
            logger.warning("Grant access attempt without user_id")
            return jsonify({"error": "Missing user_id"}), 401

        # Get request data
        data = request.get_json()
        project_id = data.get('projectId')
        if not project_id:
            return jsonify({"error": "Missing projectId"}), 400

        logger.info(f"Attempting to grant OVH access for user: {user_id}, project: {project_id}")

        # Get valid token data (auto-refreshes if expired)
        token_data = get_valid_access_token(user_id)
        if not token_data:
            logger.warning(f"Failed to get valid OVH token for user: {user_id}")
            return jsonify({
                "error": "OVH authentication expired. Please reconnect your account.",
                "action": "RECONNECT_REQUIRED"
            }), 401

        # Extract data from token
        access_token = token_data.get('access_token')
        endpoint = token_data.get('endpoint')

        if not access_token or not endpoint:
            return jsonify({"error": "Invalid OVH configuration"}), 400

        # Get API base URL
        api_base_url = OVH_API_ENDPOINTS.get(endpoint)
        if not api_base_url:
            return jsonify({"error": "Invalid OVH endpoint"}), 400

        # Try to create IAM policy automatically
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # Attempt to verify current access first
        verify_response = requests.get(
            f'{api_base_url}/cloud/project/{project_id}',
            headers=headers,
            timeout=10
        )

        if verify_response.ok:
            # User already has access
            logger.info(f"User {user_id} already has access to project {project_id}")
            return jsonify({
                "success": True,
                "message": "Access already granted"
            })

        # If we get here, user doesn't have access
        # OVH doesn't provide a simple API to grant IAM permissions automatically
        # Return manual setup steps
        logger.info(f"Auto-grant not available for user {user_id}, returning manual steps")

        manual_steps = [
            "Go to the OVH Control Panel (https://www.ovh.com/manager/)",
            "Navigate to IAM > Policies",
            "Click 'Create Policy'",
            f"Grant permissions for project: {project_id}",
            "Add 'cloudProject:read' permission",
            "Save the policy"
        ]

        return jsonify({
            "success": False,
            "manualSteps": manual_steps
        }), 403

    except Exception as e:
        logger.error(f"Error granting OVH access: {e}", exc_info=True)
        return jsonify({"error": "Failed to grant OVH access"}), 500


@ovh_bp.route('/ovh/status', methods=['GET', 'OPTIONS'])
@limiter.limit(OVH_READ_LIMITS)
def ovh_connection_status():
    """
    GET /ovh_api/ovh/status

    Check if user has an active OVH connection.

    Returns:
    {
        "connected": true | false,
        "endpoint": "ovh-eu" | null,
        "hasProjects": true | false
    }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        # Check if user has OVH credentials stored
        has_creds = has_user_credentials(user_id, 'ovh')
        
        if not has_creds:
            return jsonify({
                "connected": False,
                "endpoint": None,
                "hasProjects": False
            })

        # Get token data to check endpoint and validate
        token_data = get_valid_access_token(user_id)
        
        if not token_data:
            return jsonify({
                "connected": False,
                "endpoint": None,
                "hasProjects": False
            })

        endpoint = token_data.get('endpoint')
        access_token = token_data.get('access_token')
        
        # Validate credentials with actual API call - SINGLE SOURCE OF TRUTH
        has_projects = False
        credentials_valid = True
        try:
            api_base_url = OVH_API_ENDPOINTS.get(endpoint)
            if access_token and api_base_url:
                headers = {
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                }
                response = requests.get(
                    f'{api_base_url}/cloud/project',
                    headers=headers,
                    timeout=5
                )
                
                # Check for authentication failures (credentials actually invalid)
                if response.status_code in [401, 403]:
                    logger.warning(f"OVH API returned {response.status_code} - credentials invalid")
                    credentials_valid = False
                elif response.ok:
                    projects = response.json()
                    has_projects = len(projects) > 0
                # else: network/server error - don't delete credentials
                    
        except Exception as e:
            # Network/timeout error - don't delete credentials
            logger.warning(f"Failed to check OVH projects (network error): {e}")

        # Delete credentials ONLY if actual API call proved they're invalid
        if not credentials_valid:
            delete_user_secret(user_id, 'ovh')
            logger.warning(f"Deleted invalid OVH credentials for user {user_id}")
            return jsonify({
                "connected": False,
                "endpoint": None,
                "hasProjects": False
            })

        return jsonify({
            "connected": True,
            "endpoint": endpoint,
            "hasProjects": has_projects
        })

    except Exception as e:
        logger.error(f"Error checking OVH status: {e}", exc_info=True)
        return jsonify({"error": "Failed to check OVH status"}), 500


@ovh_bp.route('/ovh/disconnect', methods=['POST', 'OPTIONS'])
@limiter.limit("5 per minute;20 per hour")
def ovh_disconnect():
    """
    POST /ovh_api/ovh/disconnect

    Disconnect user's OVH account by removing stored credentials.

    Returns:
    {
        "status": "disconnected",
        "message": "OVH account disconnected successfully"
    }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        logger.info(f"Disconnecting OVH account for user: {user_id}")

        # Get token data to find account_id before deleting
        account_id = None
        try:
            token_data = get_valid_access_token(user_id)
            if token_data:
                # Use endpoint as account identifier for OVH
                account_id = token_data.get('endpoint', 'ovh')
        except Exception:
            account_id = 'ovh'

        # Delete credentials from Vault
        success, deleted_rows = delete_user_secret(user_id, 'ovh')
        
        if not success and deleted_rows == 0:
            logger.warning(f"No OVH credentials found to delete for user: {user_id}")
            return jsonify({
                "status": "not_connected",
                "message": "No OVH account connected"
            }), 404

        # Update connection status in user_connections table
        if account_id:
            set_connection_status(user_id, 'ovh', account_id, 'not_connected')

        logger.info(f"Successfully disconnected OVH account for user: {user_id}")
        return jsonify({
            "status": "disconnected",
            "message": "OVH account disconnected successfully"
        })

    except Exception as e:
        logger.error(f"Error disconnecting OVH account: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect OVH account"}), 500
