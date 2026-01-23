"""
Scaleway API Routes - Authentication and Projects

Provides endpoints for:
1. Connecting Scaleway account (validate + store credentials)
2. Fetching Scaleway projects
3. Connection status and disconnect

Security:
- Credentials are stored in HashiCorp Vault (not in database)
- Only secret references are stored in database
- Rate limiting applied to prevent brute force
- Input validation on all user-provided data
"""

import logging
import re
import json
import requests
from flask import request, jsonify

from routes.scaleway import scaleway_bp
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    store_user_preference,
    get_user_preference,
)
from utils.auth.token_management import store_tokens_in_db, get_token_data
from utils.secrets.secret_ref_utils import has_user_credentials, delete_user_secret
from utils.db.connection_utils import set_connection_status
from utils.web.limiter_ext import limiter
from utils.logging.secure_logging import mask_credential_value
from utils.web.cors_utils import create_cors_response
from utils.ssh.ssh_utils import (
    check_if_user_has_vms,
    delete_ssh_credentials,
    load_user_private_key_safe,
    normalize_private_key,
    parse_ssh_key_id,
    validate_private_key_format,
    validate_and_test_ssh,
)
from connectors.scaleway_connector.auth import (
    validate_scaleway_credentials,
    get_scaleway_projects,
    get_account_info
)

logger = logging.getLogger(__name__)

# Scaleway zones to check for instances
# Note: Scaleway doesn't have an API to list all available zones, so we hardcode common ones
# If a zone returns 404, it's skipped gracefully. Add new zones here as Scaleway expands.
# Based on https://www.scaleway.com/en/docs/console/availability-zones/ (as of 2025)
SCALEWAY_ZONES = [
    'fr-par-1', 'fr-par-2', 'fr-par-3',  # Paris, France
    'nl-ams-1', 'nl-ams-2', 'nl-ams-3',  # Amsterdam, Netherlands  
    'pl-waw-1', 'pl-waw-2', 'pl-waw-3',  # Warsaw, Poland
]

# Validation patterns for Scaleway credentials
ACCESS_KEY_PATTERN = re.compile(r'^SCW[A-Z0-9]{17,20}$')
SECRET_KEY_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
UUID_PATTERN = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


@scaleway_bp.route('/scaleway/connect', methods=['POST', 'OPTIONS'])
@limiter.limit("5 per minute")
def scaleway_connect():
    """
    Connect Scaleway account by validating and storing credentials.
    
    Request body:
    {
        "accessKey": "SCWXXXXXXXXXX",
        "secretKey": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "organizationId": "optional",
        "projectId": "optional"
    }
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        data = request.get_json() or {}
        access_key = data.get('accessKey') or data.get('access_key')
        secret_key = data.get('secretKey') or data.get('secret_key')
        organization_id = data.get('organizationId') or data.get('organization_id')
        project_id = data.get('projectId') or data.get('project_id')
        
        # Input validation
        if not access_key or not secret_key:
            return jsonify({"error": "Access key and secret key are required"}), 400
        
        # Validate access key format (SCW followed by alphanumeric)
        access_key = access_key.strip()
        if not ACCESS_KEY_PATTERN.match(access_key):
            logger.warning(f"Invalid access key format for user {user_id}")
            return jsonify({"error": "Invalid access key format. Should start with 'SCW' followed by alphanumeric characters."}), 400
        
        # Validate secret key format (UUID format)
        secret_key = secret_key.strip()
        if not SECRET_KEY_PATTERN.match(secret_key):
            logger.warning(f"Invalid secret key format for user {user_id}")
            return jsonify({"error": "Invalid secret key format. Should be a UUID."}), 400
        
        # Validate optional UUIDs
        if organization_id and not UUID_PATTERN.match(organization_id.strip()):
            return jsonify({"error": "Invalid organization ID format"}), 400
        if project_id and not UUID_PATTERN.match(project_id.strip()):
            return jsonify({"error": "Invalid project ID format"}), 400
        
        # Log with masked credentials for security
        logger.info(f"Scaleway connect attempt for user {user_id}, access_key: {mask_credential_value(access_key)}")
        
        # Validate credentials with Scaleway API
        success, account_info, error = validate_scaleway_credentials(
            access_key, secret_key, organization_id, project_id
        )
        
        if not success:
            logger.warning(f"Scaleway credential validation failed for user {user_id}: {error}")
            return jsonify({"error": error}), 401
        
        # Store credentials securely (secret_key goes to Vault)
        token_data = {
            "access_key": access_key,
            "secret_key": secret_key,
            "organization_id": account_info.get("organization_id"),
            "default_project_id": account_info.get("default_project_id"),
        }
        
        store_tokens_in_db(user_id, token_data, "scaleway")
        set_connection_status(user_id, "scaleway", access_key, "connected")
        
        logger.info(f"Scaleway connected successfully for user {user_id}")
        
        # Check if user has VMs to suggest vm-config redirect
        has_vms = check_if_user_has_vms(user_id, 'scaleway')
        
        response_data = {
            "success": True,
            "message": "Scaleway connected successfully",
            "organizationId": account_info.get("organization_id"),
            "projectsCount": account_info.get("projects_count", 0)
        }
        
        if has_vms:
            response_data["redirect_to"] = "/vm-config"
            response_data["has_vms"] = True
            logger.info(f"User {user_id} has Scaleway VMs, suggesting vm-config redirect")
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Error connecting Scaleway for user: {e}", exc_info=True)
        return jsonify({"error": "Failed to connect Scaleway"}), 500


@scaleway_bp.route('/scaleway/projects', methods=['GET', 'POST', 'OPTIONS'])
@limiter.limit("30 per minute")
def scaleway_projects():
    """
    Fetch or save Scaleway projects.
    
    GET Returns:
    {
        "projects": [
            {"projectId": "...", "projectName": "...", "organizationId": "..."}
        ]
    }
    
    POST Body:
    {
        "projects": [{"projectId": "...", "enabled": true/false}]
    }
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        # Get stored credentials
        token_data = get_token_data(user_id, "scaleway")
        if not token_data:
            return jsonify({
                "error": "Scaleway not connected. Please connect your account.",
                "action": "CONNECT_REQUIRED"
            }), 401
        
        if request.method == 'POST':
            # Save project selections
            data = request.get_json()
            projects = data.get("projects", [])
            
            # Store project selections in user preferences
            store_user_preference(user_id, 'scaleway_projects', projects)
            
            logger.info(f"Saved Scaleway project selections for user {user_id}")
            return jsonify({"success": True, "message": "Projects saved"})
        
        # GET - Fetch projects
        secret_key = token_data.get("secret_key")
        if not secret_key:
            return jsonify({"error": "Invalid stored credentials"}), 401
        
        # Get organization_id and access_key from stored token data
        organization_id = token_data.get("organization_id")
        access_key = token_data.get("access_key")
        
        # Fetch projects from Scaleway API
        success, projects, error = get_scaleway_projects(secret_key, organization_id, access_key)
        
        if not success:
            return jsonify({"error": error}), 400
        
        # Load saved project selections
        saved_prefs = get_user_preference(user_id, 'scaleway_projects') or []
        root_project_id = get_user_preference(user_id, 'scaleway_root_project')
        
        # Convert saved preferences list to dict for easier lookup
        saved_selections = {}
        if isinstance(saved_prefs, list):
            for p in saved_prefs:
                if isinstance(p, dict):
                    saved_selections[p.get('projectId')] = p.get('enabled', True)
        
        # Merge saved selections with fetched projects
        for project in projects:
            project_id = project.get('projectId')
            if project_id in saved_selections:
                project['enabled'] = saved_selections[project_id]
            else:
                project['enabled'] = True  # Default to enabled
            project['isRootProject'] = (project_id == root_project_id)
        
        return jsonify({"projects": projects})
        
    except Exception as e:
        logger.error(f"Error with Scaleway projects: {e}", exc_info=True)
        return jsonify({"error": "Failed to process projects request"}), 500


@scaleway_bp.route('/scaleway/status', methods=['GET', 'OPTIONS'])
@limiter.limit("60 per minute")
def scaleway_status():
    """Check Scaleway connection status and validate credentials with API call."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"connected": False, "provider": "scaleway"}), 200
        
        has_creds = has_user_credentials(user_id, "scaleway")
        if not has_creds:
            return jsonify({"connected": False, "provider": "scaleway"})
        
        # Validate credentials with actual API call - SINGLE SOURCE OF TRUTH
        token_data = get_token_data(user_id, "scaleway")
        if not token_data:
            return jsonify({"connected": False, "provider": "scaleway"})
        
        secret_key = token_data.get("secret_key")
        if not secret_key:
            return jsonify({"connected": False, "provider": "scaleway"})
        
        # Test credentials with Scaleway API
        success, _, error = get_account_info(secret_key)
        
        if not success:
            # Check if it's an authentication error (credentials invalid)
            if error and ("401" in str(error) or "403" in str(error) or "Invalid" in str(error) or "Unauthorized" in str(error)):
                logger.warning(f"Scaleway credentials invalid for user {user_id}: {error}")
                delete_user_secret(user_id, "scaleway")
                return jsonify({"connected": False, "provider": "scaleway"})
            # else: network/server error - don't delete credentials
            logger.warning(f"Scaleway API check failed (non-auth error): {error}")
        
        return jsonify({
            "connected": True,
            "provider": "scaleway"
        })
        
    except Exception as e:
        logger.error(f"Error checking Scaleway status: {e}")
        # On exception, assume connected if has creds (don't delete on errors)
        has_creds = has_user_credentials(user_id, "scaleway") if user_id else False
        return jsonify({"connected": has_creds, "provider": "scaleway"}), 200


@scaleway_bp.route('/scaleway/disconnect', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")
def scaleway_disconnect():
    """Disconnect Scaleway account."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        # Get access_key before deleting for status update
        token_data = get_token_data(user_id, "scaleway")
        access_key = token_data.get("access_key", "unknown") if token_data else "unknown"
        
        # Delete stored credentials
        delete_user_secret(user_id, "scaleway")
        set_connection_status(user_id, "scaleway", access_key, "disconnected")
        
        logger.info(f"Scaleway disconnected for user {user_id}")
        
        return jsonify({
            "success": True,
            "message": "Scaleway disconnected successfully"
        })
        
    except Exception as e:
        logger.error(f"Error disconnecting Scaleway: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Scaleway"}), 500


@scaleway_bp.route('/scaleway/root-project', methods=['GET', 'POST', 'OPTIONS'])
@limiter.limit("30 per minute")
def scaleway_root_project():
    """Get or set the root project for Scaleway."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        if request.method == 'POST':
            data = request.get_json()
            project_id = data.get("projectId")
            
            if not project_id:
                return jsonify({"error": "projectId is required"}), 400
            
            # Store the root project preference
            store_user_preference(user_id, 'scaleway_root_project', project_id)
            
            logger.info(f"Set Scaleway root project to {project_id} for user {user_id}")
            return jsonify({
                "success": True,
                "projectId": project_id,
                "message": "Root project set successfully"
            })
        
        # GET - Fetch current root project
        root_project = get_user_preference(user_id, 'scaleway_root_project')
        
        if root_project:
            return jsonify({
                "projectId": root_project,
                "hasRootProject": True
            })
        
        return jsonify({
            "projectId": None,
            "hasRootProject": False
        })
            
    except Exception as e:
        logger.error(f"Error with Scaleway root project: {e}", exc_info=True)
        return jsonify({"error": "Failed to process root project request"}), 500


@scaleway_bp.route('/scaleway/instances', methods=['GET', 'OPTIONS'])
@limiter.limit("30 per minute")
def scaleway_instances():
    """
    GET /scaleway_api/scaleway/instances - Fetch all Scaleway instances
    
    Returns:
    {
        "servers": [
            {
                "id": "server-id",
                "name": "instance-name",
                "state": "running",
                "public_ip": {"address": "1.2.3.4"},
                "zone": "fr-par-1",
                "sshConfig": null
            }
        ]
    }
    """
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        logger.info(f"Fetching Scaleway instances for user: {user_id}")
        
        token_data = get_token_data(user_id, "scaleway")
        if not token_data:
            return jsonify({"error": "Not authenticated"}), 401
        
        secret_key = token_data.get("secret_key")
        if not secret_key:
            return jsonify({"error": "Invalid credentials"}), 401
        
        all_servers = []
        
        headers = {
            'X-Auth-Token': secret_key,
            'Content-Type': 'application/json'
        }
        
        for zone in SCALEWAY_ZONES:
            try:
                response = requests.get(
                    f'https://api.scaleway.com/instance/v1/zones/{zone}/servers',
                    headers=headers,
                    timeout=5
                )
                
                logger.debug(f"Scaleway zone {zone} response: status={response.status_code}")
                
                if response.ok:
                    data = response.json()
                    servers_in_zone = data.get('servers', [])
                    logger.debug(f"Found {len(servers_in_zone)} servers in zone {zone}")
                    
                    for server in servers_in_zone:
                        logger.debug(f"Server: {server.get('id')} - {server.get('name')} - {server.get('state')}")
                        
                        # Handle image as dict with 'name' or as string ID
                        image_obj = server.get('image')
                        if isinstance(image_obj, dict):
                            image_name = image_obj.get('name', '')
                        else:
                            # If image is string or missing, fetch full server details
                            server_id = server.get('id')
                            try:
                                detail_resp = requests.get(
                                    f'https://api.scaleway.com/instance/v1/zones/{zone}/servers/{server_id}',
                                    headers=headers,
                                    timeout=3
                                )
                                if detail_resp.ok:
                                    detail_server = detail_resp.json()['server']
                                    image_name = detail_server.get('image', {}).get('name', '')
                                else:
                                    image_name = ''
                            except:
                                image_name = ''
                        
                        server_id = server.get('id')
                        
                        # Check if SSH key exists for this server
                        ssh_configured = False
                        try:
                            ssh_token_data = get_token_data(user_id, f"scaleway_ssh_{server_id}")
                            ssh_configured = bool(ssh_token_data and ssh_token_data.get('private_key'))
                            logger.debug(f"SSH key check for {server_id}: configured={ssh_configured}")
                        except Exception as e:
                            logger.debug(f"No SSH key found for server {server_id}: {e}")
                        
                        all_servers.append({
                            'id': server_id,
                            'name': server.get('name'),
                            'state': server.get('state'),
                            'public_ip': server.get('public_ip'),
                            'zone': zone,
                            'commercial_type': server.get('commercial_type'),
                            'imageName': image_name,
                            'sshConfig': ssh_configured
                        })
                elif response.status_code == 404:
                    # Zone might not be available for this account
                    logger.info(f"Zone {zone} not available (404)")
                    continue
                else:
                    logger.warning(f"Failed to fetch servers from zone {zone}: {response.status_code} - {response.text}")
            except Exception as e:
                logger.warning(f"Error fetching servers from zone {zone}: {e}")
                continue
        
        logger.info(f"Successfully fetched {len(all_servers)} Scaleway instances for user {user_id}")
        return jsonify({'servers': all_servers})
    
    except Exception as e:
        logger.error(f"Error fetching Scaleway instances: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch Scaleway instances"}), 500


@scaleway_bp.route('/scaleway/instances/<server_id>/ssh-keys', methods=['POST', 'DELETE', 'OPTIONS'])
@limiter.limit("10 per minute")
def save_scaleway_ssh_keys(server_id):
    """
    Save SSH private key for a Scaleway server
    
    POST /scaleway_api/scaleway/instances/<server_id>/ssh-keys
    
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
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401
        
        # Validate server_id format
        if not UUID_PATTERN.match(server_id):
            return jsonify({"error": "Invalid server ID format"}), 400
        
        # Handle DELETE request
        if request.method == 'DELETE':
            success, status_code, message = delete_ssh_credentials(user_id, server_id, 'scaleway')
            return jsonify({"success": success, "message": message} if success else {"error": message}), status_code
        
        # Handle POST request
        logger.info(f"Saving SSH key for Scaleway server {server_id}, user: {user_id}")
        
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
                return jsonify({"error": str(e)}), 400
        
        logger.info(f"Received private key for server {server_id}, length: {len(private_key)} chars")
        
        # Fetch server details and test SSH connection
        token_data = get_token_data(user_id, "scaleway")
        if not token_data:
            return jsonify({"error": "Not authenticated with Scaleway"}), 401
        secret_key = token_data.get("secret_key")
        headers = {'X-Auth-Token': secret_key, 'Content-Type': 'application/json'}
        
        logger.info(f"Starting SSH validation for server {server_id}")
        
        # Find server's IP and state
        server_found = False
        server_ip = None
        ssh_username = custom_username if custom_username else 'root'  # Use custom or default to root
        
        if custom_username:
            logger.info(f"Using custom SSH username: {ssh_username}")
        
        for zone in SCALEWAY_ZONES:
            try:
                resp = requests.get(f'https://api.scaleway.com/instance/v1/zones/{zone}/servers/{server_id}', 
                                   headers=headers, timeout=3)
                if resp.ok:
                    server = resp.json()['server']
                    server_found = True
                    server_ip = server.get('public_ip', {}).get('address')
                    state = server.get('state')
                    
                    logger.info(f"Found server in zone {zone}, IP: {server_ip}, state: {state}")
                    
                    if not server_ip:
                        return jsonify({"error": "Server has no public IP address"}), 400
                    
                    if state != 'running':
                        return jsonify({"error": f"Server is not running (state: {state}). Please start the server and try again."}), 400
                    
                    break
            except requests.RequestException as e:
                logger.debug(f"Zone {zone} API check: {e}")
                continue
        
        if not server_found:
            return jsonify({"error": "Server not found in any zone"}), 404
        
        # Test SSH connection using shared utility
        # Wrap in try/catch to handle race condition where VM could shut down between state check and SSH attempt
        try:
            success, error_msg, connected_as = validate_and_test_ssh(server_ip, ssh_username, private_key, timeout=30)
            
            if not success:
                return jsonify({"error": error_msg}), 400
        except Exception as e:
            logger.error(f"SSH validation failed unexpectedly: {e}", exc_info=True)
            return jsonify({"error": "SSH validation failed. The server may have become unavailable."}), 400
        
        # Store SSH key only after successful validation
        ssh_data = {
            "private_key": private_key,
            "server_id": server_id,
            "validated": True,
            "saved_at": json.dumps({"timestamp": "now"})
        }
        
        store_tokens_in_db(user_id, ssh_data, f"scaleway_ssh_{server_id}")
        
        logger.info(f"Successfully saved SSH key for server {server_id}, user {user_id}")
        return jsonify({
            "success": True,
            "message": f"SSH key validated and saved successfully (connected as {connected_as})"
        })
    
    except Exception as e:
        logger.error(f"Error saving Scaleway SSH key: {e}", exc_info=True)
        return jsonify({"error": "Failed to save SSH key"}), 500
