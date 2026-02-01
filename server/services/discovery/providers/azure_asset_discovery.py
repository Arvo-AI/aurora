"""
Azure Asset Discovery - Phase 1 provider for Azure Resource Graph.

Uses the Azure CLI `az graph query` command to discover all resources
across subscriptions via Azure Resource Graph (KQL). Maps each resource
to a normalized graph node using the resource_mapper.
"""

import json
import logging
import subprocess

from services.discovery.resource_mapper import map_azure_resource

logger = logging.getLogger(__name__)

# KQL query for Azure Resource Graph - fetches all resources with relevant fields
RESOURCE_GRAPH_QUERY = (
    "Resources "
    "| project name, type, location, resourceGroup, subscriptionId, "
    "properties, tags, identity, sku, kind"
)


def discover(user_id, credentials, env=None):
    """Discover all Azure resources using Resource Graph.

    Args:
        user_id: The user requesting discovery.
        credentials: Dict with optional keys: subscription_id, tenant_id,
                     client_id, client_secret.
        env: Unused (Azure builds its own env from credentials). Accepted for
             interface consistency with other providers.

    Returns:
        Dict with keys:
            nodes: List of normalized resource node dicts.
            relationships: Empty list (Phase 1 does not infer relationships).
            errors: List of error message strings encountered during discovery.
    """
    nodes = []
    errors = []

    try:
        resources = _query_resource_graph(credentials)
    except ResourceGraphExtensionError as exc:
        logger.error("Azure Resource Graph extension not installed: %s", exc)
        return {
            "nodes": [],
            "relationships": [],
            "errors": [str(exc)],
        }
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        logger.error("Azure CLI failed (exit %d): %s", exc.returncode, stderr)
        return {
            "nodes": [],
            "relationships": [],
            "errors": [f"Azure CLI query failed: {stderr}"],
        }
    except Exception as exc:
        logger.error("Unexpected error during Azure discovery: %s", exc, exc_info=True)
        return {
            "nodes": [],
            "relationships": [],
            "errors": [f"Azure discovery error: {exc}"],
        }

    for resource in resources:
        try:
            node = _resource_to_node(resource)
            if node is not None:
                nodes.append(node)
        except Exception as exc:
            resource_name = resource.get("name", "<unknown>")
            resource_type = resource.get("type", "<unknown>")
            msg = f"Failed to process Azure resource {resource_name} ({resource_type}): {exc}"
            logger.warning(msg)
            errors.append(msg)

    logger.info(
        "Azure discovery for user %s: found %d resources, mapped %d nodes, %d errors",
        user_id,
        len(resources),
        len(nodes),
        len(errors),
    )

    return {
        "nodes": nodes,
        "relationships": [],
        "errors": errors,
    }


class ResourceGraphExtensionError(Exception):
    """Raised when the Azure Resource Graph CLI extension is not installed."""
    pass


def _query_resource_graph(credentials):
    """Execute an Azure Resource Graph query via the Azure CLI.

    Args:
        credentials: Dict with optional subscription_id, tenant_id,
                     client_id, client_secret.

    Returns:
        List of resource dicts from Resource Graph.

    Raises:
        ResourceGraphExtensionError: If the resource-graph extension is missing.
        subprocess.CalledProcessError: If the az command fails.
    """
    # Azure CLI requires an explicit login — env vars alone are not enough.
    _az_login(credentials)

    cmd = [
        "az", "graph", "query",
        "-q", RESOURCE_GRAPH_QUERY,
        "--output", "json",
    ]

    subscription_id = credentials.get("subscription_id")
    if subscription_id:
        cmd.extend(["--subscriptions", subscription_id])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except FileNotFoundError:
        raise ResourceGraphExtensionError(
            "Azure CLI (az) is not installed or not found on PATH. "
            "Install it from https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "resource-graph" in stderr or "'graph' is not" in stderr:
            raise ResourceGraphExtensionError(
                "Azure Resource Graph CLI extension is not installed. "
                "Run: az extension add --name resource-graph"
            )
        raise

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse Azure CLI JSON output: {exc}")

    # Resource Graph returns results under a "data" key
    if isinstance(output, dict):
        return output.get("data", output.get("Data", []))
    if isinstance(output, list):
        return output
    return []


def _az_login(credentials):
    """Log in to Azure CLI using service principal credentials.

    Required before running az graph query since env vars alone
    are not sufficient for Azure CLI authentication.
    """
    tenant_id = credentials.get("tenant_id")
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")

    if not (tenant_id and client_id and client_secret):
        logger.warning("Azure credentials incomplete, skipping az login")
        return

    cmd = [
        "az", "login",
        "--service-principal",
        "--username", client_id,
        "--password", client_secret,
        "--tenant", tenant_id,
        "--output", "none",
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        logger.info("Azure CLI login successful for tenant %s", tenant_id)
    except subprocess.CalledProcessError as exc:
        logger.error("Azure CLI login failed: %s", exc.stderr)
        raise


def _resource_to_node(resource):
    """Convert a single Azure Resource Graph result to a normalized node dict.

    Args:
        resource: Dict from Resource Graph with name, type, location, etc.

    Returns:
        Normalized node dict, or None if the resource type is unmapped.
    """
    azure_type = resource.get("type", "")
    kind = resource.get("kind")
    name = resource.get("name", "")
    location = resource.get("location", "")
    resource_group = resource.get("resourceGroup", "")
    subscription_id = resource.get("subscriptionId", "")
    properties = resource.get("properties") or {}
    tags = resource.get("tags") or {}
    sku = resource.get("sku") or {}
    identity = resource.get("identity") or {}

    # Map Azure type (+ kind for web/sites) to normalized types
    resource_type, sub_type = map_azure_resource(azure_type, kind=kind)
    if resource_type is None:
        logger.debug("Unmapped Azure resource type: %s (kind=%s)", azure_type, kind)
        return None

    # Build the full Azure resource ID
    cloud_resource_id = (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/{azure_type}/{name}"
    )

    # Try to extract an endpoint from properties
    endpoint = _extract_endpoint(azure_type, properties)

    # Try to extract VNet association; fall back to resource group grouping
    vpc_id = _extract_vnet_id(properties)
    if not vpc_id and subscription_id and resource_group:
        vpc_id = f"azure-{subscription_id}/{resource_group}"

    node = {
        "name": name,
        "display_name": name,
        "resource_type": resource_type,
        "sub_type": sub_type,
        "provider": "azure",
        "region": location,
        "cloud_resource_id": cloud_resource_id,
        "endpoint": endpoint,
        "vpc_id": vpc_id,
        "metadata": {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "azure_type": azure_type,
            "kind": kind,
            "tags": tags,
            "sku": sku,
            "identity_type": identity.get("type"),
        },
    }

    return node


def _extract_endpoint(azure_type, properties):
    """Extract an endpoint URL or hostname from resource properties.

    Different Azure resource types store their endpoints in different
    property fields. This function checks the most common ones.

    Returns:
        Endpoint string or None.
    """
    if not properties:
        return None

    normalized_type = azure_type.lower()

    # Web apps and function apps
    if normalized_type == "microsoft.web/sites":
        hostnames = properties.get("defaultHostName") or properties.get("hostNames")
        if isinstance(hostnames, list) and hostnames:
            return f"https://{hostnames[0]}"
        if isinstance(hostnames, str):
            return f"https://{hostnames}"

    # SQL servers
    if "microsoft.sql/servers" in normalized_type:
        fqdn = properties.get("fullyQualifiedDomainName")
        if fqdn:
            return fqdn

    # Cosmos DB
    if normalized_type == "microsoft.documentdb/databaseaccounts":
        endpoint = properties.get("documentEndpoint")
        if endpoint:
            return endpoint

    # Redis Cache
    if normalized_type == "microsoft.cache/redis":
        hostname = properties.get("hostName")
        port = properties.get("sslPort") or properties.get("port")
        if hostname:
            return f"{hostname}:{port}" if port else hostname

    # Storage accounts
    if normalized_type == "microsoft.storage/storageaccounts":
        endpoints = properties.get("primaryEndpoints") or {}
        return endpoints.get("blob") or endpoints.get("web")

    # API Management
    if normalized_type == "microsoft.apimanagement/service":
        return properties.get("gatewayUrl")

    # AKS
    if normalized_type == "microsoft.containerservice/managedclusters":
        return properties.get("fqdn")

    # PostgreSQL / MySQL flexible servers
    if "microsoft.dbfor" in normalized_type:
        fqdn = properties.get("fullyQualifiedDomainName")
        if fqdn:
            return fqdn

    return None


def _extract_vnet_id(properties):
    """Extract a VNet resource ID from properties if present.

    Various Azure resources reference their VNet/subnet in different
    property paths. Returns the VNet ID or None.
    """
    if not properties:
        return None

    # Check common patterns for VNet/subnet references
    # AKS agent pool profiles
    agent_pools = properties.get("agentPoolProfiles") or []
    if isinstance(agent_pools, list):
        for pool in agent_pools:
            vnet_subnet_id = pool.get("vnetSubnetID")
            if vnet_subnet_id:
                # Extract VNet ID from subnet ID
                # Format: /subscriptions/.../virtualNetworks/VNET/subnets/SUBNET
                parts = vnet_subnet_id.split("/subnets/")
                if len(parts) == 2:
                    return parts[0]
                return vnet_subnet_id

    # Network profile with subnet
    network_profile = properties.get("networkProfile") or {}
    subnet_id = network_profile.get("subnetId")
    if subnet_id:
        parts = subnet_id.split("/subnets/")
        if len(parts) == 2:
            return parts[0]
        return subnet_id

    # Direct subnet reference (e.g., App Service VNet integration)
    virtual_network_subnet_id = properties.get("virtualNetworkSubnetId")
    if virtual_network_subnet_id:
        parts = virtual_network_subnet_id.split("/subnets/")
        if len(parts) == 2:
            return parts[0]
        return virtual_network_subnet_id

    return None
