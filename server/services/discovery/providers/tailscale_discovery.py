"""
Tailscale Discovery Provider - Discovers devices and DNS config via Tailscale REST API.
Uses the Tailscale API v2 with Bearer token authentication.
"""

import logging

import requests

logger = logging.getLogger(__name__)

PROVIDER = "tailscale"
BASE_URL = "https://api.tailscale.com"
REQUEST_TIMEOUT = 60


def _api_get(endpoint, api_key, timeout=REQUEST_TIMEOUT):
    """Make an authenticated GET request to the Tailscale API.

    Args:
        endpoint: API path (e.g. /api/v2/tailnet/example.com/devices).
        api_key: Tailscale API key for Bearer auth.
        timeout: Request timeout in seconds.

    Returns:
        Tuple of (parsed_json_dict, error_string_or_None).
    """
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json(), None
    except requests.exceptions.Timeout:
        return None, f"Request timed out after {timeout}s: {url}"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        body = e.response.text[:500] if e.response is not None else ""
        return None, f"HTTP {status} from {url}: {body}"
    except requests.exceptions.RequestException as e:
        return None, f"Request failed for {url}: {e}"
    except ValueError as e:
        return None, f"Failed to parse JSON from {url}: {e}"


def _extract_device_node(device):
    """Extract a graph node dict from a Tailscale device."""
    device_id = device.get("id") or device.get("nodeId") or device.get("name", "")
    name = device.get("name", device_id)

    # Determine sub_type: subnet routers get a special sub_type
    is_subnet_router = bool(device.get("enabledRoutes"))
    os_field = device.get("os", "")
    if is_subnet_router:
        sub_type = "subnet_router"
    elif os_field:
        sub_type = os_field.lower()
    else:
        sub_type = "tailscale_device"

    # Extract Tailscale IP (first address in the addresses array)
    addresses = device.get("addresses", [])
    endpoint = addresses[0] if addresses else None

    node = {
        "id": f"tailscale:{device_id}",
        "name": name,
        "resource_type": "on_prem_device",
        "sub_type": sub_type,
        "provider": PROVIDER,
        "raw": device,
    }

    if endpoint:
        node["endpoint"] = endpoint

    # Add hostname if available
    hostname = device.get("hostname")
    if hostname:
        node["hostname"] = hostname

    # Add OS info
    if os_field:
        node["os"] = os_field

    # Add last seen / online status
    last_seen = device.get("lastSeen")
    if last_seen:
        node["last_seen"] = last_seen

    is_online = device.get("online")
    if is_online is not None:
        node["status"] = "online" if is_online else "offline"

    # Tag subnet routes on the node
    enabled_routes = device.get("enabledRoutes", [])
    if enabled_routes:
        node["enabled_routes"] = enabled_routes

    return node


def _build_network_relationships(nodes):
    """Build weak network dependency edges between devices on the same tailnet.

    All devices on the same tailnet can reach each other, so we create
    bidirectional edges with low confidence.

    Args:
        nodes: List of device node dicts.

    Returns:
        List of relationship dicts.
    """
    relationships = []
    if len(nodes) < 2:
        return relationships

    # Create pairwise edges (only one direction to avoid duplicates)
    for i, source in enumerate(nodes):
        for target in nodes[i + 1:]:
            relationships.append({
                "from_service": source["name"],
                "to_service": target["name"],
                "dependency_type": "network",
                "confidence": 0.5,
                "discovered_from": "tailscale_tailnet",
            })

    return relationships


def discover(user_id, credentials, env=None):
    """Discover Tailscale devices and network configuration.

    Args:
        user_id: The user performing discovery.
        credentials: Dict with keys:
            - api_key: Tailscale API key (required).
            - tailnet: Tailnet name or domain (required).
        env: Unused (Tailscale uses REST API, not subprocess). Accepted for
             interface consistency with other providers.

    Returns:
        Dict with keys: nodes (list), relationships (list), errors (list).
    """
    nodes = []
    relationships = []
    errors = []

    api_key = credentials.get("api_key")
    tailnet = credentials.get("tailnet")

    if not api_key:
        error_msg = "Tailscale discovery: api_key is required"
        logger.error(error_msg)
        return {"nodes": [], "relationships": [], "errors": [error_msg]}

    if not tailnet:
        error_msg = "Tailscale discovery: tailnet is required"
        logger.error(error_msg)
        return {"nodes": [], "relationships": [], "errors": [error_msg]}

    # Discover devices
    logger.info(f"Tailscale discovery: listing devices for tailnet {tailnet}, user {user_id}")
    devices_data, error = _api_get(f"/api/v2/tailnet/{tailnet}/devices", api_key)

    if error:
        error_msg = f"Failed to list Tailscale devices: {error}"
        logger.warning(error_msg)
        errors.append(error_msg)
    else:
        devices = devices_data.get("devices", [])
        for device in devices:
            node = _extract_device_node(device)
            nodes.append(node)
        logger.info(f"Tailscale discovery: found {len(devices)} devices")

        # Build network edges between devices on the same tailnet
        relationships.extend(_build_network_relationships(nodes))

    # Discover DNS nameservers (informational, attached to node metadata)
    logger.info(f"Tailscale discovery: fetching DNS config for tailnet {tailnet}")
    dns_data, error = _api_get(f"/api/v2/tailnet/{tailnet}/dns/nameservers", api_key)

    if error:
        error_msg = f"Failed to fetch Tailscale DNS config: {error}"
        logger.warning(error_msg)
        errors.append(error_msg)
    else:
        nameservers = dns_data.get("dns", []) or dns_data.get("nameservers", [])
        if nameservers:
            logger.info(f"Tailscale discovery: found {len(nameservers)} DNS nameservers")
            # Attach DNS config as a separate node for visibility
            dns_node = {
                "id": f"tailscale:dns:{tailnet}",
                "name": f"{tailnet} DNS config",
                "resource_type": "dns_zone",
                "sub_type": "tailscale_dns",
                "provider": PROVIDER,
                "nameservers": nameservers,
                "raw": dns_data,
            }
            nodes.append(dns_node)

    logger.info(
        f"Tailscale discovery complete for user {user_id}: "
        f"{len(nodes)} nodes, {len(relationships)} relationships, {len(errors)} errors"
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
        "errors": errors,
    }
