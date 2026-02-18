"""
OVH OAuth2 Authorization Code Flow Implementation

This module implements the standard OAuth2 authorization code flow for OVH,
providing a streamlined user experience similar to "Sign in with Google".

Flow:
1. User clicks "Connect with OVH" button
2. Backend redirects to OVH authorization endpoint
3. User logs in to OVH and authorizes Aurora
4. OVH redirects back with authorization code
5. Backend exchanges code for access + refresh tokens
6. Tokens stored securely in HashiCorp Vault

This flow provides significantly better UX than bootstrap flow:
- Bootstrap: 14 steps, 2 minutes, 3 copy/pastes
- Authorization Code: 5 steps, 30 seconds, 0 copy/paste
"""
import logging
import secrets
import json
import hashlib
import base64
import time
import os
from typing import Dict, Optional
from flask import request, jsonify, redirect
import requests
from routes.ovh import ovh_bp
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import store_tokens_in_db
from urllib.parse import urlencode
from utils.auth.oauth2_state_cache import store_oauth2_state, retrieve_oauth2_state
from connectors.ovh_connector.oauth2_config import get_oauth2_config
from utils.db.connection_utils import save_connection_metadata

# Import limiter for rate limiting
from utils.web.limiter_ext import limiter
from config.rate_limiting import OVH_OAUTH2_LIMITS, OVH_READ_LIMITS

logger = logging.getLogger(__name__)

# OAuth2 Authorization Endpoints
# NOTE: These endpoints are VERIFIED as working in production (Jan 2025), but are NOT
# officially documented by OVHcloud. The authorization code flow is mentioned as supported
# in OVH documentation, but implementation details are not provided in official guides.
# Third-party implementations (e.g., carsso/oauth2-ovhcloud PHP package) confirm functionality.
# Status: Working and stable, pending official documentation from OVH.
OAUTH2_AUTHORIZE_ENDPOINTS = {
    'ovh-eu': 'https://www.ovh.com/auth/oauth2/authorize',
    'ovh-us': 'https://us.ovhcloud.com/auth/oauth2/authorize',
    'ovh-ca': 'https://ca.ovh.com/auth/oauth2/authorize',
}

# OAuth2 Token Endpoints (documented)
OAUTH2_TOKEN_ENDPOINTS = {
    'ovh-eu': 'https://www.ovh.com/auth/oauth2/token',
    'ovh-us': 'https://us.ovhcloud.com/auth/oauth2/token',
    'ovh-ca': 'https://ca.ovh.com/auth/oauth2/token',
}


@ovh_bp.route('/ovh/oauth2/initiate', methods=['POST', 'OPTIONS'])
@limiter.limit("5 per minute;20 per hour;100 per day")
def ovh_oauth2_initiate():
    """
    Initiate OAuth2 authorization code flow.

    Redirects user to OVH authorization endpoint where they log in
    and authorize Aurora to access their account.

    Request body:
    {
        "endpoint": "ovh-eu" | "ovh-us" | "ovh-ca",
        "projectId": "optional-project-id"
    }

    Returns:
    {
        "authorizationUrl": "https://www.ovh.com/auth/oauth2/authorize?..."
    }
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        from utils.web.cors_utils import create_cors_response
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            logger.warning("OAuth2 initiation attempt without user_id")
            return jsonify({"error": "Missing user_id"}), 401

        data = request.get_json() or {}
        endpoint = data.get('endpoint')
        project_id = data.get('projectId')

        if not endpoint:
            return jsonify({
                "error": "Missing endpoint. Must be one of: ovh-eu, ovh-us, ovh-ca"
            }), 400

        if endpoint not in OAUTH2_AUTHORIZE_ENDPOINTS:
            return jsonify({
                "error": f"Invalid endpoint '{endpoint}'. Must be one of: ovh-eu, ovh-us, ovh-ca"
            }), 400

        # Generate CSRF state token for security
        state = secrets.token_urlsafe(32)

        # Generate PKCE code_verifier and code_challenge (S256)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode().rstrip('=')

        # Store state in cache instead of session (no cookie issues this way)
        # This avoids SameSite/Secure cookie conflicts
        store_oauth2_state(state, user_id, endpoint, project_id, code_verifier)

        # Get OAuth2 client config for this region
        oauth2_config = get_oauth2_config()
        client_config = oauth2_config.get(endpoint)
        if not client_config or not client_config.get('client_id'):
            return jsonify({
                "error": f"OAuth2 not configured for {endpoint}",
                "hint": "Please configure OVH_*_CLIENT_ID and OVH_*_CLIENT_SECRET environment variables"
            }), 500

        # Build authorization URL
        authorize_url = OAUTH2_AUTHORIZE_ENDPOINTS[endpoint]
        params = {
            'client_id': client_config['client_id'],
            'redirect_uri': client_config['redirect_uri'],
            'response_type': 'code',
            'scope': 'all',  # Request all available scopes
            'state': state,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256',
        }

        # Construct full authorization URL with proper URL encoding
        auth_url_full = f"{authorize_url}?{urlencode(params)}"

        logger.info(f"Initiated OAuth2 flow for user {user_id}, endpoint {endpoint}")
        return jsonify({
            "authorizationUrl": auth_url_full,
            "state": state
        })

    except Exception as e:
        logger.error(f"OAuth2 initiation error: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate OAuth2 flow"}), 500


@ovh_bp.route('/ovh/oauth2/callback', methods=['GET', 'POST', 'OPTIONS'])
@limiter.limit("5 per minute;20 per hour;100 per day")
def ovh_oauth2_callback():
    """
    Handle OAuth2 authorization callback.

    After user authorizes Aurora on OVH, they're redirected back with an
    authorization code. We exchange this code for access and refresh tokens.

    GET requests (from OVH redirect): Processes and redirects to frontend
    POST requests (programmatic): Returns JSON response

    Query/Body params:
        code: authorization-code-from-ovh
        state: csrf-state-token
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        from utils.web.cors_utils import create_cors_response
        return create_cors_response()
    
    # Determine frontend URL for redirects
    frontend_url = os.environ.get('FRONTEND_URL')
    is_get_request = request.method == 'GET'
    
    try:
        # Support both GET (standard OAuth2) and POST (programmatic)
        if is_get_request:
            code = request.args.get('code')
            state = request.args.get('state')
        else:
            data = request.get_json() or {}
            code = data.get('code')
            state = data.get('state')

        if not code or not state:
            if is_get_request:
                return redirect(f"{frontend_url}/ovh/onboarding?error=missing_params")
            return jsonify({"error": "Missing code or state parameter"}), 400

        # Validate state token (CSRF protection)
        # Using cache instead of session to avoid cookie domain/SameSite issues
        state_data = retrieve_oauth2_state(state)
        if not state_data:
            logger.warning(f"Invalid or expired OAuth2 state token: {state}")
            if is_get_request:
                return redirect(f"{frontend_url}/ovh/onboarding?error=invalid_state")
            return jsonify({"error": "Invalid or expired state token"}), 400

        # Extract stored data (expiration already validated by cache)
        user_id = state_data['user_id']
        endpoint = state_data['endpoint']
        project_id = state_data.get('project_id')
        code_verifier = state_data.get('code_verifier')

        # Get OAuth2 client config
        oauth2_config = get_oauth2_config()
        client_config = oauth2_config.get(endpoint)
        if not client_config or not client_config.get('client_id'):
            if is_get_request:
                return redirect(f"{frontend_url}/ovh/onboarding?error=config_error")
            return jsonify({"error": f"OAuth2 not configured for {endpoint}"}), 500

        # Exchange authorization code for tokens
        token_url = OAUTH2_TOKEN_ENDPOINTS[endpoint]
        logger.info(f"Exchanging authorization code for tokens at {token_url}")
        logger.info(f"Using redirect_uri: {client_config['redirect_uri']}")
        logger.info(f"Using client_id: {client_config['client_id']}")

        # Prepare token exchange data - OVH expects credentials in form data
        token_data = {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_config['client_id'],
            'client_secret': client_config['client_secret'],
            'redirect_uri': client_config['redirect_uri'],
        }
        
        # Add PKCE code_verifier if present
        if state_data.get('code_verifier'):
            token_data['code_verifier'] = state_data['code_verifier']
        
        token_response = requests.post(
            token_url,
            data=token_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if not token_response.ok:
            # Log only status code and error type, not full response body (may contain secrets)
            error_message = f"Token exchange failed: {token_response.status_code}"
            error_code = None
            try:
                error_details = token_response.json()
                error_code = error_details.get('error')
                error_description = error_details.get('error_description', '')
                logger.error(f"OVH token exchange error: status={token_response.status_code}, error={error_code}, description={error_description}")
                if error_code:
                    error_message = error_code
            except Exception:
                logger.error(f"OVH token exchange error: status={token_response.status_code} (non-JSON response)")

            if is_get_request:
                return redirect(f"{frontend_url}/ovh/onboarding?error={error_code or 'token_exchange_failed'}")
            return jsonify({
                "error": error_message,
                "hint": "Authorization code may have already been used" if error_code == "invalid_grant" else "Please try connecting again"
            }), 500

        token_data = token_response.json()
        access_token = token_data.get('access_token')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 3600)

        if not access_token:
            logger.error("No access_token in token response")
            if is_get_request:
                return redirect(f"{frontend_url}/ovh/onboarding?error=no_access_token")
            return jsonify({"error": "No access_token received from OVH"}), 500

        logger.info(f"Successfully exchanged code for tokens (has_refresh: {bool(refresh_token)})")

        # Store tokens in Vault
        token_storage = {
            "endpoint": endpoint,
            "client_id": client_config['client_id'],  # Store client_id for IAM validation
            "access_token": access_token,
            "token_type": token_data.get('token_type', 'Bearer'),
            "expires_at": int(time.time()) + expires_in,
            "auth_method": "authorization_code",
        }

        if refresh_token:
            token_storage["refresh_token"] = refresh_token
            logger.info("Refresh token received and will be stored")
        else:
            logger.warning("No refresh token received - user may need to re-authorize after expiration")

        if project_id:
            token_storage["projectId"] = project_id

        # Store tokens
        store_tokens_in_db(user_id, token_storage, 'ovh')
        logger.info(f"OAuth2 authorization code flow completed successfully for user {user_id}")

        # Save connection metadata for user_connections tracking
        try:
            save_connection_metadata(
                user_id=user_id,
                provider='ovh',
                account_id=endpoint,  # Use endpoint (ovh-eu, ovh-us, ovh-ca) as account identifier
                connection_method='oauth2_authorization_code',
                status='active'
            )
            logger.info(f"Saved OVH connection metadata for user {user_id}")
        except Exception as meta_err:
            logger.warning(f"Failed to save connection metadata (non-critical): {meta_err}")

        # For GET requests (browser redirect from OVH), redirect to frontend
        # Use 'login' param for consistency with GCP/AWS/Azure OAuth flow
        # Check if user has VMs and redirect to vm-config page if they do
        if is_get_request:
            from utils.ssh.ssh_utils import check_if_user_has_vms
            has_vms = check_if_user_has_vms(user_id, 'ovh')
            if has_vms:
                logger.info(f"User {user_id} has OVH VMs, redirecting to vm-config page")
                return redirect(f"{frontend_url}/vm-config?provider=ovh&connected=true&login=ovh_success")
            return redirect(f"{frontend_url}/chat?login=ovh_success")
        
        # For POST requests (programmatic), return JSON
        return jsonify({
            "status": "success",
            "message": "OVH account connected successfully",
            "endpoint": endpoint,
            "hasRefreshToken": bool(refresh_token),
            "expiresIn": expires_in
        })

    except requests.exceptions.Timeout:
        logger.error("Token exchange request timed out")
        if request.method == 'GET':
            frontend_url = os.environ.get('FRONTEND_URL')
            return redirect(f"{frontend_url}/ovh/onboarding?error=timeout")
        return jsonify({"error": "Token exchange timed out"}), 504
    except requests.exceptions.RequestException as e:
        logger.error("Token exchange request error", exc_info=True)
        if request.method == 'GET':
            frontend_url = os.environ.get('FRONTEND_URL')
            return redirect(f"{frontend_url}/ovh/onboarding?error=network_error")
        return jsonify({"error": "Network error during token exchange"}), 500
    except Exception as e:
        logger.error("OAuth2 callback error", exc_info=True)
        if request.method == 'GET':
            frontend_url = os.environ.get('FRONTEND_URL')
            return redirect(f"{frontend_url}/ovh/onboarding?error=callback_failed")
        return jsonify({"error": "OAuth2 callback failed"}), 500


@ovh_bp.route('/ovh/oauth2/refresh', methods=['POST'])
@limiter.limit("5 per minute;20 per hour;100 per day")
def ovh_oauth2_refresh():
    """
    Refresh expired OAuth2 access token using refresh token.

    Called automatically when access token expires.

    Returns:
    {
        "status": "success",
        "expiresIn": 3600
    }
    """
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Missing user_id"}), 401

        # Get current token data
        from utils.secrets.secret_ref_utils import get_user_token_data
        token_data = get_user_token_data(user_id, 'ovh')

        if not token_data:
            return jsonify({"error": "No OVH credentials found"}), 404

        refresh_token = token_data.get('refresh_token')
        if not refresh_token:
            return jsonify({
                "error": "No refresh token available",
                "hint": "User needs to re-authorize via OAuth2 flow"
            }), 400

        endpoint = token_data.get('endpoint', 'ovh-eu')
        oauth2_config = get_oauth2_config()
        client_config = oauth2_config.get(endpoint)
        if not client_config or not client_config.get('client_id'):
            return jsonify({"error": f"OAuth2 not configured for {endpoint}"}), 500

        # Request new access token using refresh token
        token_url = OAUTH2_TOKEN_ENDPOINTS[endpoint]
        logger.info(f"Refreshing access token for user {user_id}")

        refresh_response = requests.post(
            token_url,
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': client_config['client_id'],
                'client_secret': client_config['client_secret'],
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if not refresh_response.ok:
            # Removed: from utils.logging.secure_logging import safe_error_log
            logger.error(f"Token refresh failed: {refresh_response.status_code}")
            return jsonify({
                "error": f"Token refresh failed: {refresh_response.status_code}",
                "hint": "User may need to re-authorize"
            }), 500

        new_token_data = refresh_response.json()
        new_access_token = new_token_data.get('access_token')
        new_refresh_token = new_token_data.get('refresh_token', refresh_token)  # Keep old if not rotated
        expires_in = new_token_data.get('expires_in', 3600)

        if not new_access_token:
            return jsonify({"error": "No access_token in refresh response"}), 500

        # Update stored tokens
        token_data['access_token'] = new_access_token
        token_data['refresh_token'] = new_refresh_token
        token_data['expires_at'] = int(time.time()) + expires_in

        store_tokens_in_db(user_id, token_data, 'ovh')
        logger.info(f"Successfully refreshed access token for user {user_id}")

        return jsonify({
            "status": "success",
            "expiresIn": expires_in
        })

    except requests.exceptions.Timeout:
        logger.error("Token refresh request timed out")
        return jsonify({"error": "Token refresh timed out"}), 504
    except requests.exceptions.RequestException as e:
        # Removed: from utils.logging.secure_logging import safe_error_log
        logger.error("Token refresh request error", exc_info=True)
        return jsonify({"error": "Network error during token refresh"}), 500
    except Exception as e:
        # Removed: from utils.logging.secure_logging import safe_error_log
        logger.error("Token refresh error", exc_info=True)
        return jsonify({"error": "Token refresh failed"}), 500


def get_valid_access_token(user_id: str) -> Optional[Dict]:
    """
    Get valid token data for OVH API calls, refreshing if necessary.

    This is a helper function that checks token expiration and automatically
    refreshes if needed before returning the full token data.

    Args:
        user_id: User ID

    Returns:
        Full token data dict with valid access_token, or None if refresh fails
    """
    try:
        from utils.secrets.secret_ref_utils import get_user_token_data

        token_data = get_user_token_data(user_id, 'ovh')
        if not token_data:
            return None

        # Check if this is authorization code flow (has expires_at)
        expires_at = token_data.get('expires_at')
        if not expires_at:
            # Not authorization code flow, return token_data as-is
            return token_data

        # Check if token is expired or expiring soon (within 5 minutes - larger buffer for reliability)
        now = int(time.time())
        if expires_at > now + 300:
            # Token still valid for >5 min
            return token_data

        # Token expired or expiring soon, need to refresh
        logger.info(f"Access token expired or expiring soon for user {user_id}, refreshing")

        refresh_token = token_data.get('refresh_token')
        if not refresh_token:
            logger.warning(f"No refresh token available for user {user_id}")
            # Return old token anyway - let API calls determine if it's invalid
            return token_data

        endpoint = token_data.get('endpoint', 'ovh-eu')
        oauth2_config = get_oauth2_config()
        client_config = oauth2_config.get(endpoint)
        if not client_config or not client_config.get('client_id'):
            logger.error(f"OAuth2 not configured for {endpoint} - returning existing token")
            # Server config issue - return existing token, don't fail
            return token_data

        # Try to refresh the token
        try:
            token_url = OAUTH2_TOKEN_ENDPOINTS[endpoint]
            refresh_response = requests.post(
                token_url,
                data={
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh_token,
                    'client_id': client_config['client_id'],
                    'client_secret': client_config['client_secret'],
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )

            if refresh_response.ok:
                new_token_data = refresh_response.json()
                new_access_token = new_token_data.get('access_token')
                if new_access_token:
                    # Update stored tokens
                    token_data['access_token'] = new_access_token
                    token_data['refresh_token'] = new_token_data.get('refresh_token', refresh_token)
                    token_data['expires_at'] = now + new_token_data.get('expires_in', 3600)
                    store_tokens_in_db(user_id, token_data, 'ovh')
                    logger.info(f"Successfully auto-refreshed token for user {user_id}")
                    return token_data
            
            # Refresh failed - log but return old token
            # Status endpoint will delete credentials if API calls fail with 401/403
            logger.warning(f"Token refresh failed ({refresh_response.status_code}) - returning existing token")
            return token_data

        except Exception as refresh_err:
            # Network error - return old token anyway
            logger.warning(f"Token refresh error: {refresh_err} - returning existing token")
            return token_data

    except Exception as e:
        logger.error("Error getting valid access token", exc_info=True)
        # Return None only if no token data at all
        return None
