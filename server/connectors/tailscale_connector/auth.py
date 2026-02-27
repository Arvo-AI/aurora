"""
Tailscale authentication and credential validation.

Implements OAuth 2.0 Client Credentials flow for Tailscale API access.

Handles:
- OAuth token exchange (client credentials flow)
- Token refresh (tokens expire after 1 hour)
- Credential validation
- Tailnet discovery
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"
TAILSCALE_TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"


def get_oauth_token(client_id: str, client_secret: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Exchange OAuth client credentials for an access token.

    Tailscale OAuth tokens expire after 1 hour and must be refreshed
    by requesting a new token (no refresh_token in client credentials flow).

    Args:
        client_id: Tailscale OAuth client ID
        client_secret: Tailscale OAuth client secret

    Returns:
        Tuple of (success, token_data, error_message)
        token_data includes: access_token, token_type, expires_in, expires_at
    """
    if not client_id or not client_secret:
        return False, None, "Client ID and client secret are required"

    try:
        response = requests.post(
            TAILSCALE_TOKEN_URL,
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if response.status_code == 401:
            return False, None, "Invalid credentials. Please check your client ID and secret."

        if response.status_code == 403:
            return False, None, "Access denied. OAuth client may lack required scopes."

        if not response.ok:
            error_detail = ""
            try:
                error_data = response.json()
                error_detail = error_data.get('error_description', error_data.get('error', ''))
            except Exception:
                error_detail = response.text[:200]
            return False, None, f"Token request failed ({response.status_code}): {error_detail}"

        data = response.json()
        access_token = data.get('access_token')

        if not access_token:
            return False, None, "No access token in response"

        expires_in = data.get('expires_in', 3600)
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        token_data = {
            'access_token': access_token,
            'token_type': data.get('token_type', 'Bearer'),
            'expires_in': expires_in,
            'expires_at': expires_at.isoformat(),
        }

        logger.info(f"Tailscale OAuth token obtained (expires in {expires_in}s)")
        return True, token_data, None

    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"Tailscale token request failed: {e}")
        return False, None, "Connection error. Please check your network and try again."
    except Exception as e:
        logger.error(f"Unexpected error getting Tailscale token: {e}")
        return False, None, "An unexpected error occurred. Please try again."


def refresh_oauth_token(client_id: str, client_secret: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Refresh OAuth token by requesting a new one.

    In client credentials flow, there's no refresh_token - we simply
    request a new access_token using the same credentials.

    Args:
        client_id: Tailscale OAuth client ID
        client_secret: Tailscale OAuth client secret

    Returns:
        Tuple of (success, token_data, error_message)
    """
    return get_oauth_token(client_id, client_secret)


def validate_tailscale_credentials(
    client_id: str,
    client_secret: str,
    tailnet: Optional[str] = None
) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate Tailscale OAuth credentials by fetching tailnet info.

    Args:
        client_id: Tailscale OAuth client ID
        client_secret: Tailscale OAuth client secret
        tailnet: Optional tailnet name (uses '-' for default if not provided)

    Returns:
        Tuple of (success, account_info, error_message)
        account_info includes: tailnet, tailnet_name, device_count, token_data
    """
    # First get an access token
    success, token_data, error = get_oauth_token(client_id, client_secret)
    if not success:
        return False, None, error

    access_token = token_data['access_token']
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Use '-' to auto-select the user's tailnet if not specified
    target_tailnet = tailnet or "-"

    try:
        # Fetch devices to validate access and get tailnet info
        response = requests.get(
            f"{TAILSCALE_API_BASE}/tailnet/{target_tailnet}/devices",
            headers=headers,
            timeout=15
        )

        if response.status_code == 401:
            return False, None, "Invalid or expired token"

        if response.status_code == 403:
            return False, None, "Access denied. OAuth client may lack 'devices' scope."

        if response.status_code == 404:
            return False, None, f"Tailnet '{target_tailnet}' not found or not accessible"

        if not response.ok:
            return False, None, f"API error: {response.status_code}"

        data = response.json()
        devices = data.get('devices', [])

        # Try to get tailnet name from DNS preferences
        tailnet_name = target_tailnet
        try:
            dns_response = requests.get(
                f"{TAILSCALE_API_BASE}/tailnet/{target_tailnet}/dns/preferences",
                headers=headers,
                timeout=10
            )
            if dns_response.ok:
                dns_data = dns_response.json()
                # Extract tailnet name from MagicDNS domain if available
                magic_dns_name = dns_data.get('magicDNSName', '')
                if magic_dns_name:
                    tailnet_name = magic_dns_name.split('.')[0] if '.' in magic_dns_name else magic_dns_name
        except Exception as e:
            logger.debug(f"Could not fetch DNS preferences: {e}")

        account_info = {
            'client_id': client_id,
            'client_secret': client_secret,
            'tailnet': target_tailnet,
            'tailnet_name': tailnet_name,
            'device_count': len(devices),
            'devices': devices[:5],  # Include first 5 devices for preview
            'token_data': token_data,
        }

        logger.info(f"Tailscale credentials validated. Tailnet: {tailnet_name}, Devices: {len(devices)}")
        return True, account_info, None

    except requests.exceptions.Timeout:
        return False, None, "Connection timeout. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"Tailscale API request failed: {e}")
        return False, None, "Connection error. Please check your network and try again."
    except Exception as e:
        logger.error(f"Unexpected error validating Tailscale credentials: {e}")
        return False, None, "An unexpected error occurred. Please try again."


def get_user_tailnets(access_token: str) -> Tuple[bool, List[Dict], Optional[str]]:
    """
    List all tailnets accessible to the user.

    Note: Tailscale API currently doesn't have a direct endpoint to list
    all tailnets. Users typically have access to one tailnet per organization.
    This function attempts to get tailnet info from the default tailnet.

    Args:
        access_token: Valid Tailscale access token

    Returns:
        Tuple of (success, tailnets_list, error_message)
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        # Get default tailnet info
        response = requests.get(
            f"{TAILSCALE_API_BASE}/tailnet/-/devices",
            headers=headers,
            timeout=15
        )

        if not response.ok:
            if response.status_code == 401:
                return False, [], "Invalid or expired token"
            return False, [], f"API error: {response.status_code}"

        data = response.json()
        devices = data.get('devices', [])

        # Extract tailnet info from devices
        tailnets = []
        seen_tailnets = set()

        for device in devices:
            # Device names contain tailnet: hostname.tailnet.ts.net
            fqdn = device.get('name', '')
            parts = fqdn.split('.')
            if len(parts) >= 3:
                tailnet_name = parts[1]
                if tailnet_name not in seen_tailnets:
                    seen_tailnets.add(tailnet_name)
                    tailnets.append({
                        'id': tailnet_name,
                        'name': tailnet_name,
                        'deviceCount': sum(1 for d in devices if tailnet_name in d.get('name', '')),
                        'enabled': True,
                        'isRootTailnet': len(tailnets) == 0  # First one is root
                    })

        # If no tailnets found from device names, create a default entry
        if not tailnets:
            tailnets.append({
                'id': '-',
                'name': 'Default Tailnet',
                'deviceCount': len(devices),
                'enabled': True,
                'isRootTailnet': True
            })

        return True, tailnets, None

    except requests.exceptions.Timeout:
        return False, [], "Connection timeout"
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch Tailscale tailnets: {e}")
        return False, [], "Failed to fetch tailnets"
    except Exception as e:
        logger.error(f"Unexpected error fetching tailnets: {e}")
        return False, [], "Failed to fetch tailnets"


def get_valid_access_token(
    client_id: str,
    client_secret: str,
    existing_token: Optional[Dict] = None
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Get a valid access token, refreshing if necessary.

    Args:
        client_id: Tailscale OAuth client ID
        client_secret: Tailscale OAuth client secret
        existing_token: Optional existing token data with expires_at

    Returns:
        Tuple of (success, access_token, error_message)
    """
    # Check if existing token is still valid (with 5-minute buffer)
    if existing_token and existing_token.get('access_token'):
        expires_at_str = existing_token.get('expires_at')
        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                buffer = timedelta(minutes=5)
                if datetime.utcnow() < (expires_at - buffer):
                    logger.debug("Using existing valid Tailscale token")
                    return True, existing_token['access_token'], None
            except Exception as e:
                logger.debug(f"Could not parse token expiry: {e}")

    # Get new token
    success, token_data, error = get_oauth_token(client_id, client_secret)
    if success:
        return True, token_data['access_token'], None
    return False, None, error
