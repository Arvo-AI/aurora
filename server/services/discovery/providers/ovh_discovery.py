"""
OVH Discovery Provider - Discovers infrastructure resources via OVH CLI commands.
Uses individual `ovh` CLI commands with subprocess to enumerate cloud project resources.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

PROVIDER = "ovh"

# Commands that require --service-name PROJECT_ID (cloud project commands)
CLOUD_COMMANDS = [
    {
        "resource_type": "vm",
        "sub_type": "ovh_instance",
        "command": ["ovhcloud", "cloud", "instance", "list", "--json"],
        "label": "instances",
        "cloud_project": True,
    },
    {
        "resource_type": "kubernetes_cluster",
        "sub_type": "ovh_kube",
        "command": ["ovhcloud", "cloud", "kube", "list", "--json"],
        "label": "kubernetes clusters",
        "cloud_project": True,
    },
    {
        "resource_type": "database",
        "sub_type": "ovh_managed_db",
        "command": ["ovhcloud", "cloud", "database-service", "list", "--json"],
        "label": "managed databases",
        "cloud_project": True,
    },
    {
        "resource_type": "load_balancer",
        "sub_type": "ovh_lb",
        "command": ["ovhcloud", "cloud", "loadbalancer", "list", "--json"],
        "label": "load balancers",
        "cloud_project": True,
    },
    {
        "resource_type": "container_registry",
        "sub_type": "ovh_container_registry",
        "command": ["ovhcloud", "cloud", "container-registry", "list", "--json"],
        "label": "container registries",
        "cloud_project": True,
    },
    {
        "resource_type": "vpc",
        "sub_type": "ovh_private_network",
        "command": ["ovhcloud", "cloud", "network", "private", "list", "--json"],
        "label": "private networks",
        "cloud_project": True,
    },
]

# Commands that do NOT require --cloud-project (account-level)
ACCOUNT_COMMANDS = [
    {
        "resource_type": "vm",
        "sub_type": "ovh_dedicated",
        "command": ["ovhcloud", "baremetal", "list", "--json"],
        "label": "dedicated servers",
        "cloud_project": False,
    },
]

ALL_COMMANDS = CLOUD_COMMANDS + ACCOUNT_COMMANDS


def _run_command(command, timeout=60, env=None):
    """Run a CLI command and return parsed JSON output.

    Args:
        command: List of command arguments.
        timeout: Timeout in seconds.
        env: Optional environment dict for subprocess (for authentication).

    Returns:
        Tuple of (parsed_json_list, error_string_or_None).
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
            return [], error_msg
        if not result.stdout.strip():
            return [], None
        data = json.loads(result.stdout)
        if data is None:
            return [], None
        if isinstance(data, list):
            return [item for item in data if item is not None], None
        return [data], None
    except subprocess.TimeoutExpired:
        return [], f"Command timed out after {timeout}s: {' '.join(command)}"
    except json.JSONDecodeError as e:
        return [], f"Failed to parse JSON from command {' '.join(command)}: {e}"
    except Exception as e:
        return [], f"Error running command {' '.join(command)}: {e}"


def _get_ovh_project_id(env=None):
    """Auto-discover the OVH cloud project ID by listing projects."""
    cmd = ["ovhcloud", "cloud", "project", "list", "--json"]
    projects, error = _run_command(cmd, env=env)
    if error:
        logger.warning(f"OVH discovery: failed to list projects: {error}")
        return None
    if projects and isinstance(projects, list):
        # Use the first project
        first = projects[0] if projects else None
        if isinstance(first, dict):
            return first.get("project_id") or first.get("Project_id") or first.get("projectId")
        if isinstance(first, str):
            return first
    return None


def _build_command(cmd_config, project_id):
    """Build the full command list, appending --cloud-project for cloud project commands."""
    command = list(cmd_config["command"])
    if cmd_config.get("cloud_project") and project_id:
        command.extend(["--cloud-project", project_id])
    return command


def _extract_node(resource, cmd_config, region=None):
    """Extract a graph node dict from a raw OVH resource."""
    resource_id = (
        resource.get("id")
        or resource.get("serviceName")
        or resource.get("name")
        or str(resource)
    )
    name = resource.get("name") or resource.get("displayName") or resource_id

    node = {
        "id": f"ovh:{resource_id}",
        "name": name,
        "resource_type": cmd_config["resource_type"],
        "sub_type": cmd_config["sub_type"],
        "provider": PROVIDER,
        "raw": resource,
    }

    # Add region if available from resource or credentials
    resource_region = resource.get("region") or region
    if resource_region:
        node["region"] = resource_region

    # Add status if available
    status = resource.get("status") or resource.get("state")
    if status:
        node["status"] = status

    return node


def discover(user_id, credentials, env=None):
    """Discover OVH infrastructure resources.

    Args:
        user_id: The user performing discovery.
        credentials: Dict with keys:
            - project_id: OVH cloud project service name (required for cloud commands).
            - region: Optional default region filter.
        env: Optional environment dict for subprocess calls (for authentication).

    Returns:
        Dict with keys: nodes (list), relationships (list), errors (list).
    """
    nodes = []
    relationships = []
    errors = []

    project_id = credentials.get("project_id")
    region = credentials.get("region")

    # Auto-discover project ID if not provided
    if not project_id:
        project_id = _get_ovh_project_id(env)
        if project_id:
            logger.info(f"OVH discovery: auto-discovered project_id: {project_id}")
        else:
            logger.warning("OVH discovery: no project_id found, skipping cloud project commands")

    for cmd_config in ALL_COMMANDS:
        # Skip cloud project commands if no project_id
        if cmd_config.get("cloud_project") and not project_id:
            continue

        command = _build_command(cmd_config, project_id)
        label = cmd_config["label"]

        logger.info(f"OVH discovery: listing {label} for user {user_id}")
        resources, error = _run_command(command, env=env)

        if error:
            error_msg = f"Failed to list OVH {label}: {error}"
            logger.warning(error_msg)
            errors.append(error_msg)
            continue

        for resource in resources:
            node = _extract_node(resource, cmd_config, region)
            nodes.append(node)

        logger.info(f"OVH discovery: found {len(resources)} {label}")

    logger.info(
        f"OVH discovery complete for user {user_id}: "
        f"{len(nodes)} nodes, {len(errors)} errors"
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
        "errors": errors,
    }
