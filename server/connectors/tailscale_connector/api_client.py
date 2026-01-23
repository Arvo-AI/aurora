"""
Tailscale API client wrapper.

Provides methods for interacting with the Tailscale API:
- Device management (list, authorize, remove, tag)
- ACL policy management
- Auth key generation
- DNS configuration
- Route management
"""

import json
import logging
import requests
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"


class TailscaleClient:
    """
    Tailscale API client.

    Handles all interactions with the Tailscale API using a provided
    access token. Token refresh is handled externally.
    """

    def __init__(self, access_token: str):
        """
        Initialize the Tailscale client.

        Args:
            access_token: Valid OAuth access token
        """
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"  # Request JSON instead of HuJSON
        }
        self.timeout = 30

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None
    ) -> Tuple[bool, Optional[Any], Optional[str]]:
        """
        Make an API request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            endpoint: API endpoint (without base URL)
            data: Optional JSON body
            params: Optional query parameters

        Returns:
            Tuple of (success, response_data, error_message)
        """
        url = f"{TAILSCALE_API_BASE}{endpoint}"

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                json=data,
                params=params,
                timeout=self.timeout
            )

            if response.status_code == 204:
                return True, None, None

            if response.status_code == 401:
                return False, None, "Unauthorized. Token may be expired."

            if response.status_code == 403:
                return False, None, "Forbidden. Insufficient permissions."

            if response.status_code == 404:
                return False, None, "Resource not found."

            if not response.ok:
                error_msg = f"API error ({response.status_code})"
                try:
                    error_data = response.json()
                    if 'message' in error_data:
                        error_msg = error_data['message']
                except Exception:
                    pass
                return False, None, error_msg

            # Some endpoints return empty response
            if not response.text:
                return True, None, None

            # Parse JSON response
            try:
                return True, response.json(), None
            except (json.JSONDecodeError, ValueError) as json_err:
                logger.error(f"Failed to parse Tailscale API response: {json_err}")
                logger.debug(f"Response text: {response.text[:500]}")
                return False, None, f"Invalid response from Tailscale API"

        except requests.exceptions.Timeout:
            return False, None, "Request timeout"
        except requests.exceptions.RequestException as e:
            logger.error(f"Tailscale API request failed: {e}")
            return False, None, f"Connection error: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error in Tailscale API request: {e}")
            return False, None, str(e)

    # =========================================================================
    # Device Management
    # =========================================================================

    def list_devices(self, tailnet: str = "-") -> Tuple[bool, List[Dict], Optional[str]]:
        """
        List all devices in a tailnet.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, devices_list, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/devices")
        if not success:
            return False, [], error

        devices = data.get('devices', []) if data else []

        # Format devices for frontend
        formatted = []
        for device in devices:
            formatted.append({
                'id': device.get('id', ''),
                'nodeId': device.get('nodeId', ''),
                'hostname': device.get('hostname', ''),
                'name': device.get('name', ''),
                'addresses': device.get('addresses', []),
                'authorized': device.get('authorized', False),
                'blocked': device.get('blocked', False),
                'tags': device.get('tags', []),
                'lastSeen': device.get('lastSeen', ''),
                'os': device.get('os', ''),
                'clientVersion': device.get('clientVersion', ''),
                'updateAvailable': device.get('updateAvailable', False),
                'isExitNode': device.get('enabledRoutes', {}).get('exit-node', False) if isinstance(device.get('enabledRoutes'), dict) else False,
                'isSubnetRouter': len(device.get('advertisedRoutes', [])) > 0,
                'advertisedRoutes': device.get('advertisedRoutes', []),
                'enabledRoutes': device.get('enabledRoutes', []),
                'user': device.get('user', ''),
                'created': device.get('created', ''),
                'expires': device.get('expires', ''),
            })

        return True, formatted, None

    def get_device(self, device_id: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get details for a specific device.

        Args:
            device_id: Device ID

        Returns:
            Tuple of (success, device_data, error_message)
        """
        success, data, error = self._request("GET", f"/device/{device_id}")
        return success, data, error

    def authorize_device(self, device_id: str) -> Tuple[bool, Optional[str]]:
        """
        Authorize a device to join the tailnet.

        Args:
            device_id: Device ID

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/device/{device_id}/authorized",
            data={"authorized": True}
        )
        return success, error

    def delete_device(self, device_id: str) -> Tuple[bool, Optional[str]]:
        """
        Remove a device from the tailnet.

        Args:
            device_id: Device ID

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request("DELETE", f"/device/{device_id}")
        return success, error

    def set_device_tags(self, device_id: str, tags: List[str]) -> Tuple[bool, Optional[str]]:
        """
        Set tags on a device.

        Args:
            device_id: Device ID
            tags: List of tags (must be prefixed with 'tag:')

        Returns:
            Tuple of (success, error_message)
        """
        # Ensure tags are properly formatted
        formatted_tags = []
        for tag in tags:
            if not tag.startswith('tag:'):
                formatted_tags.append(f"tag:{tag}")
            else:
                formatted_tags.append(tag)

        success, _, error = self._request(
            "POST",
            f"/device/{device_id}/tags",
            data={"tags": formatted_tags}
        )
        return success, error

    def set_device_routes(
        self,
        device_id: str,
        routes: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Enable routes for a device (subnet router).

        Args:
            device_id: Device ID
            routes: List of CIDR routes to enable

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/device/{device_id}/routes",
            data={"routes": routes}
        )
        return success, error

    def set_device_key_expiry(
        self,
        device_id: str,
        key_expiry_disabled: bool
    ) -> Tuple[bool, Optional[str]]:
        """
        Enable or disable key expiry for a device.

        Args:
            device_id: Device ID
            key_expiry_disabled: True to disable key expiry

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/device/{device_id}/key",
            data={"keyExpiryDisabled": key_expiry_disabled}
        )
        return success, error

    # =========================================================================
    # ACL Management
    # =========================================================================

    def get_acl(self, tailnet: str = "-") -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get the current ACL policy.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, acl_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/acl")
        return success, data, error

    def update_acl(
        self,
        tailnet: str,
        acl: Dict,
        if_match: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Update the ACL policy.

        Args:
            tailnet: Tailnet name
            acl: New ACL policy
            if_match: Optional ETag for optimistic locking

        Returns:
            Tuple of (success, updated_acl, error_message)
        """
        headers = self.headers.copy()
        if if_match:
            headers['If-Match'] = if_match

        try:
            response = requests.post(
                f"{TAILSCALE_API_BASE}/tailnet/{tailnet}/acl",
                headers=headers,
                json=acl,
                timeout=self.timeout
            )

            if not response.ok:
                error_msg = f"ACL update failed ({response.status_code})"
                try:
                    error_data = response.json()
                    if 'message' in error_data:
                        error_msg = error_data['message']
                except Exception:
                    pass
                return False, None, error_msg

            return True, response.json(), None

        except Exception as e:
            logger.error(f"ACL update error: {e}")
            return False, None, str(e)

    def preview_acl(
        self,
        tailnet: str,
        acl: Dict
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Preview ACL changes without applying.

        Args:
            tailnet: Tailnet name
            acl: ACL policy to preview

        Returns:
            Tuple of (success, preview_result, error_message)
        """
        success, data, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/acl/preview",
            data=acl
        )
        return success, data, error

    def validate_acl(
        self,
        tailnet: str,
        acl: Dict
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Validate ACL syntax without applying.

        Args:
            tailnet: Tailnet name
            acl: ACL policy to validate

        Returns:
            Tuple of (success, validation_result, error_message)
        """
        success, data, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/acl/validate",
            data=acl
        )
        return success, data, error

    # =========================================================================
    # Auth Keys
    # =========================================================================

    def list_auth_keys(self, tailnet: str = "-") -> Tuple[bool, List[Dict], Optional[str]]:
        """
        List all auth keys in a tailnet.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, keys_list, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/keys")
        if not success:
            return False, [], error

        keys = data.get('keys', []) if data else []
        return True, keys, None

    def create_auth_key(
        self,
        tailnet: str = "-",
        reusable: bool = False,
        ephemeral: bool = False,
        preauthorized: bool = True,
        tags: Optional[List[str]] = None,
        expiry_seconds: Optional[int] = None,
        description: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Create a new auth key.

        Args:
            tailnet: Tailnet name or '-' for default
            reusable: Whether the key can be used multiple times
            ephemeral: Whether devices using this key are ephemeral
            preauthorized: Whether devices are auto-authorized
            tags: Tags to apply to devices using this key
            expiry_seconds: Key expiry in seconds (default: 90 days)
            description: Optional description

        Returns:
            Tuple of (success, key_data, error_message)
            key_data includes the actual key value (only shown once)
        """
        capabilities = {
            "devices": {
                "create": {
                    "reusable": reusable,
                    "ephemeral": ephemeral,
                    "preauthorized": preauthorized,
                }
            }
        }

        if tags:
            # Ensure tags are properly formatted
            formatted_tags = []
            for tag in tags:
                if not tag.startswith('tag:'):
                    formatted_tags.append(f"tag:{tag}")
                else:
                    formatted_tags.append(tag)
            capabilities["devices"]["create"]["tags"] = formatted_tags

        body: Dict[str, Any] = {"capabilities": capabilities}

        if expiry_seconds:
            body["expirySeconds"] = expiry_seconds

        if description:
            body["description"] = description

        success, data, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/keys",
            data=body
        )
        return success, data, error

    def get_auth_key(self, tailnet: str, key_id: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get details for an auth key.

        Args:
            tailnet: Tailnet name
            key_id: Auth key ID

        Returns:
            Tuple of (success, key_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/keys/{key_id}")
        return success, data, error

    def delete_auth_key(self, tailnet: str, key_id: str) -> Tuple[bool, Optional[str]]:
        """
        Delete an auth key.

        Args:
            tailnet: Tailnet name
            key_id: Auth key ID

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request("DELETE", f"/tailnet/{tailnet}/keys/{key_id}")
        return success, error

    # =========================================================================
    # DNS Configuration
    # =========================================================================

    def get_dns_nameservers(self, tailnet: str = "-") -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get DNS nameserver configuration.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, dns_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/dns/nameservers")
        return success, data, error

    def set_dns_nameservers(
        self,
        tailnet: str,
        nameservers: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Set DNS nameservers.

        Args:
            tailnet: Tailnet name
            nameservers: List of DNS server IPs

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/dns/nameservers",
            data={"dns": nameservers}
        )
        return success, error

    def get_dns_preferences(self, tailnet: str = "-") -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get DNS preferences (MagicDNS settings).

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, preferences_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/dns/preferences")
        return success, data, error

    def set_dns_preferences(
        self,
        tailnet: str,
        magic_dns: bool
    ) -> Tuple[bool, Optional[str]]:
        """
        Set DNS preferences.

        Args:
            tailnet: Tailnet name
            magic_dns: Enable/disable MagicDNS

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/dns/preferences",
            data={"magicDNS": magic_dns}
        )
        return success, error

    def get_dns_searchpaths(self, tailnet: str = "-") -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get DNS search paths.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, searchpaths_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/dns/searchpaths")
        return success, data, error

    def set_dns_searchpaths(
        self,
        tailnet: str,
        searchpaths: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Set DNS search paths.

        Args:
            tailnet: Tailnet name
            searchpaths: List of search domains

        Returns:
            Tuple of (success, error_message)
        """
        success, _, error = self._request(
            "POST",
            f"/tailnet/{tailnet}/dns/searchpaths",
            data={"searchPaths": searchpaths}
        )
        return success, error

    # =========================================================================
    # Routes
    # =========================================================================

    def get_routes(self, tailnet: str = "-") -> Tuple[bool, List[Dict], Optional[str]]:
        """
        Get all routes in a tailnet (from all devices).

        This aggregates route information from all devices.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, routes_list, error_message)
        """
        # Get devices first to aggregate routes
        success, devices, error = self.list_devices(tailnet)
        if not success:
            return False, [], error

        routes = []
        for device in devices:
            advertised = device.get('advertisedRoutes', [])
            enabled_routes = device.get('enabledRoutes', [])
            # enabledRoutes can be a dict (for exit-node) or a list
            enabled = enabled_routes if isinstance(enabled_routes, list) else []

            for route in advertised:
                routes.append({
                    'route': route,
                    'deviceId': device.get('id'),
                    'deviceName': device.get('name'),
                    'hostname': device.get('hostname'),
                    'advertised': True,
                    'enabled': route in enabled,
                })

        return True, routes, None

    # =========================================================================
    # Tailnet Settings
    # =========================================================================

    def get_tailnet_settings(self, tailnet: str = "-") -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get tailnet-wide settings.

        Args:
            tailnet: Tailnet name or '-' for default

        Returns:
            Tuple of (success, settings_data, error_message)
        """
        success, data, error = self._request("GET", f"/tailnet/{tailnet}/settings")
        return success, data, error

def create_reusable_auth_key_for_aurora(
    access_token: str,
    tailnet: str,
    expiry_seconds: int = 86400 * 90  # 90 days default
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a reusable, persistent auth key for Aurora to join the tailnet.

    This key is used for Aurora's terminal pods to join the user's tailnet.
    - reusable=True: Same key can be used across pod restarts/sessions
    - ephemeral=False: Device PERSISTS in tailnet (user must manually remove)

    Behavior:
    - One device per user (hostname based on user_id hash)
    - Device created once, stays in user's Tailscale forever
    - Pods come and go, but device entry remains
    - User can manually remove from Tailscale admin if desired

    Args:
        access_token: Valid Tailscale OAuth access token
        tailnet: Tailnet name
        expiry_seconds: Key expiry in seconds (default: 90 days)

    Returns:
        Tuple of (success, auth_key, error_message)
    """
    # First, ensure tag:aurora exists in ACL (auto-add if missing)
    tag_success, tag_error = ensure_aurora_tag_in_acl(access_token, tailnet)
    if not tag_success:
        logger.warning(f"Could not ensure tag:aurora in ACL: {tag_error}")
        # Continue anyway - might already exist or user might have added it manually

    client = TailscaleClient(access_token)

    success, data, error = client.create_auth_key(
        tailnet=tailnet,
        reusable=True,  # Can be reused across pod restarts/sessions
        ephemeral=False,  # Device PERSISTS - only removed manually by user
        preauthorized=True,
        tags=["tag:aurora"],  # Requires ACL: "tagOwners": {"tag:aurora": ["autogroup:admin"]}
        expiry_seconds=expiry_seconds,
        description="Aurora platform"
    )

    if not success:
        return False, None, error

    key = data.get('key') if data else None
    if not key:
        return False, None, "Auth key not returned in response"

    return True, key, None


def ensure_aurora_tag_in_acl(access_token: str, tailnet: str) -> Tuple[bool, Optional[str]]:
    """
    Ensure the tag:aurora exists in the Tailscale ACL tagOwners.

    Automatically adds the tag if it doesn't exist, allowing Aurora to create
    auth keys with this tag.

    Args:
        access_token: Valid Tailscale OAuth access token
        tailnet: Tailnet name

    Returns:
        Tuple of (success, error_message)
    """
    client = TailscaleClient(access_token)

    # Get current ACL
    success, acl_data, error = client.get_acl(tailnet)
    if not success:
        logger.warning(f"Failed to get ACL: {error}")
        return False, f"Failed to get ACL: {error}"

    if not acl_data:
        logger.warning("ACL data is empty")
        return False, "ACL data is empty"

    # Check if tag:aurora already exists in tagOwners
    tag_owners = acl_data.get("tagOwners", {})
    if "tag:aurora" in tag_owners:
        logger.info("tag:aurora already exists in ACL")
        return True, None

    # Add tag:aurora to tagOwners
    logger.info("Adding tag:aurora to ACL tagOwners")
    tag_owners["tag:aurora"] = ["autogroup:admin"]
    acl_data["tagOwners"] = tag_owners

    # Update ACL
    success, _, error = client.update_acl(tailnet, acl_data)
    if not success:
        logger.warning(f"Failed to update ACL: {error}")
        return False, f"Failed to update ACL: {error}"

    logger.info("Successfully added tag:aurora to ACL")
    return True, None
