"""
Scaleway Discovery Provider - Discovers infrastructure resources via Scaleway CLI commands.
Uses individual `scw` CLI commands with subprocess to enumerate resources.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

PROVIDER = "scaleway"

COMMANDS = [
    {
        "resource_type": "vm",
        "sub_type": "scw_instance",
        "command": ["scw", "instance", "server", "list", "-o", "json"],
        "label": "instances",
    },
    {
        "resource_type": "vm",
        "sub_type": "scw_baremetal",
        "command": ["scw", "baremetal", "server", "list", "-o", "json"],
        "label": "bare metal servers",
    },
    {
        "resource_type": "kubernetes_cluster",
        "sub_type": "scw_kapsule",
        "command": ["scw", "k8s", "cluster", "list", "-o", "json"],
        "label": "kubernetes clusters",
    },
    {
        "resource_type": "database",
        "sub_type": "scw_rdb",
        "command": ["scw", "rdb", "instance", "list", "-o", "json"],
        "label": "managed databases",
    },
    {
        "resource_type": "load_balancer",
        "sub_type": "scw_lb",
        "command": ["scw", "lb", "lb", "list", "-o", "json"],
        "label": "load balancers",
    },
    {
        "resource_type": "storage_bucket",
        "sub_type": "scw_object_storage",
        "command": ["scw", "object", "bucket", "list", "-o", "json"],
        "label": "object storage buckets",
    },
    {
        "resource_type": "serverless_function",
        "sub_type": "scw_function",
        "command": ["scw", "function", "function", "list", "-o", "json"],
        "label": "serverless functions",
    },
    {
        "resource_type": "serverless_function",
        "sub_type": "scw_container",
        "command": ["scw", "container", "container", "list", "-o", "json"],
        "label": "serverless containers",
    },
    {
        "resource_type": "vpc",
        "sub_type": "scw_vpc",
        "command": ["scw", "vpc", "list", "-o", "json"],
        "label": "VPCs",
    },
    {
        "resource_type": "firewall",
        "sub_type": "scw_security_group",
        "command": ["scw", "instance", "security-group", "list", "-o", "json"],
        "label": "security groups",
    },
]


def _run_command(command, timeout=60):
    """Run a CLI command and return parsed JSON output.

    Args:
        command: List of command arguments.
        timeout: Timeout in seconds.

    Returns:
        Tuple of (parsed_json_list, error_string_or_None).
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
            return [], error_msg
        if not result.stdout.strip():
            return [], None
        data = json.loads(result.stdout)
        if isinstance(data, list):
            return data, None
        # Scaleway sometimes wraps results in a key (e.g. {"servers": [...]})
        # Try to extract the first list value
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return value, None
            return [data], None
        return [data], None
    except subprocess.TimeoutExpired:
        return [], f"Command timed out after {timeout}s: {' '.join(command)}"
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON from command {' '.join(command)}: {e}"
    except Exception as e:
        return [], f"Error running command {' '.join(command)}: {e}"


def _extract_node(resource, cmd_config):
    """Extract a graph node dict from a raw Scaleway resource."""
    resource_id = (
        resource.get("id")
        or resource.get("ID")
        or resource.get("name")
        or str(resource)
    )
    name = resource.get("name") or resource.get("Name") or resource_id

    node = {
        "id": f"scaleway:{resource_id}",
        "name": name,
        "resource_type": cmd_config["resource_type"],
        "sub_type": cmd_config["sub_type"],
        "provider": PROVIDER,
        "raw": resource,
    }

    # Add region/zone if available
    zone = resource.get("zone") or resource.get("Zone")
    region = resource.get("region") or resource.get("Region")
    if zone:
        node["zone"] = zone
    if region:
        node["region"] = region

    # Add status if available
    status = resource.get("status") or resource.get("state") or resource.get("Status")
    if status:
        node["status"] = status

    # Add endpoint if available
    endpoint = resource.get("public_ip") or resource.get("endpoint")
    if endpoint:
        if isinstance(endpoint, dict):
            endpoint = endpoint.get("address") or endpoint.get("ip")
        node["endpoint"] = endpoint

    return node


def discover(user_id, credentials):
    """Discover Scaleway infrastructure resources.

    Args:
        user_id: The user performing discovery.
        credentials: Dict with keys (used for scw CLI configuration):
            - region: Optional default region.

    Returns:
        Dict with keys: nodes (list), relationships (list), errors (list).
    """
    nodes = []
    relationships = []
    errors = []

    for cmd_config in COMMANDS:
        command = list(cmd_config["command"])
        label = cmd_config["label"]

        logger.info(f"Scaleway discovery: listing {label} for user {user_id}")
        resources, error = _run_command(command)

        if error:
            error_msg = f"Failed to list Scaleway {label}: {error}"
            logger.warning(error_msg)
            errors.append(error_msg)
            continue

        for resource in resources:
            node = _extract_node(resource, cmd_config)
            nodes.append(node)

        logger.info(f"Scaleway discovery: found {len(resources)} {label}")

    logger.info(
        f"Scaleway discovery complete for user {user_id}: "
        f"{len(nodes)} nodes, {len(errors)} errors"
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
        "errors": errors,
    }
