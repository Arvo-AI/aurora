"""
GCP Cloud Asset Inventory Discovery Provider.

Uses gcloud CLI commands to bulk-discover all GCP resources via the
Cloud Asset Inventory API and maps them into normalized graph nodes
and dependency edges for Aurora's infrastructure dependency graph.
"""

import json
import logging
import subprocess

from services.discovery.resource_mapper import map_gcp_resource, GCP_RELATIONSHIP_TYPE_MAP

logger = logging.getLogger(__name__)


def _run_command(args, timeout=120, env=None):
    """Run a gcloud CLI command and return parsed JSON output.

    Args:
        args: List of command arguments (e.g. ["gcloud", "asset", ...]).
        timeout: Command timeout in seconds.
        env: Optional environment dict for subprocess (for authentication).

    Returns:
        Parsed JSON output from the command, or None on failure.

    Raises:
        RuntimeError: If the command fails with a recognizable error.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Cloud Asset API has not been used" in stderr or \
               ("cloudasset.googleapis.com" in stderr and "is not enabled" in stderr):
                raise RuntimeError(
                    "Cloud Asset API is not enabled for this project. "
                    "Enable it with: gcloud services enable cloudasset.googleapis.com"
                )
            logger.error(f"gcloud command failed (rc={result.returncode}): {stderr}")
            return None

        output = result.stdout.strip()
        if not output:
            return []

        return json.loads(output)

    except subprocess.TimeoutExpired:
        logger.error(f"gcloud command timed out after {timeout}s: {' '.join(args)}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse gcloud JSON output: {e}")
        return None


def _extract_name(resource_path):
    """Extract the resource name from a GCP resource path.

    Examples:
        "projects/my-proj/zones/us-central1-a/instances/api-server" -> "api-server"
        "//storage.googleapis.com/my-bucket" -> "my-bucket"
    """
    if not resource_path:
        return ""
    return resource_path.rstrip("/").rsplit("/", 1)[-1]


def _extract_region_zone(asset):
    """Extract region and zone from a GCP asset.

    Checks the 'location' field first, then falls back to parsing the
    resource name path for zones/regions segments.

    Returns:
        (region, zone) tuple. Either may be None.
    """
    location = asset.get("location", "")
    name_path = asset.get("name", "")

    region = None
    zone = None

    # Check explicit location field
    if location:
        if location.count("-") == 2:
            # Zone format: us-central1-a
            zone = location
            region = location.rsplit("-", 1)[0]
        elif location.count("-") == 1:
            # Region format: us-central1
            region = location
        else:
            # Could be "global" or a multi-region like "us"
            region = location

    # Fall back to parsing the name path
    if not region and name_path:
        parts = name_path.split("/")
        for i, part in enumerate(parts):
            if part == "zones" and i + 1 < len(parts):
                zone = parts[i + 1]
                region = zone.rsplit("-", 1)[0]
                break
            elif part == "regions" and i + 1 < len(parts):
                region = parts[i + 1]
                break
            elif part == "locations" and i + 1 < len(parts):
                region = parts[i + 1]
                break

    return region, zone


def _extract_vpc_id(asset):
    """Extract VPC ID from network-related properties when available.

    Looks in additionalAttributes and resource.data for network references.
    """
    # Try additionalAttributes first
    additional = asset.get("additionalAttributes", {})
    if isinstance(additional, dict):
        for key in ("network", "networkRef", "networkUri", "vpcNetwork"):
            val = additional.get(key)
            if val:
                return _extract_name(val)

    # Try resource.data for nested resource representations
    resource_data = asset.get("resource", {})
    if isinstance(resource_data, dict):
        data = resource_data.get("data", {})
        if isinstance(data, dict):
            # Compute instances have networkInterfaces
            network_interfaces = data.get("networkInterfaces", [])
            if network_interfaces and isinstance(network_interfaces, list):
                first_iface = network_interfaces[0]
                if isinstance(first_iface, dict):
                    network = first_iface.get("network", "")
                    if network:
                        return _extract_name(network)

            # Direct network field
            for key in ("network", "networkUri"):
                val = data.get(key)
                if val:
                    return _extract_name(val)

    return None


def _extract_endpoint(asset):
    """Try to extract a meaningful endpoint/IP from the asset."""
    resource_data = asset.get("resource", {})
    if isinstance(resource_data, dict):
        data = resource_data.get("data", {})
        if isinstance(data, dict):
            # Check for common endpoint fields
            for key in ("uri", "url", "selfLink", "httpsTrigger"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
                elif isinstance(val, dict):
                    # httpsTrigger.url for Cloud Functions
                    url = val.get("url")
                    if url:
                        return url

            # Cloud Run has status.url
            status = data.get("status", {})
            if isinstance(status, dict):
                url = status.get("url")
                if url:
                    return url

            # Compute instances: natIP or networkIP
            network_interfaces = data.get("networkInterfaces", [])
            if network_interfaces and isinstance(network_interfaces, list):
                iface = network_interfaces[0]
                if isinstance(iface, dict):
                    access_configs = iface.get("accessConfigs", [])
                    if access_configs and isinstance(access_configs, list):
                        nat_ip = access_configs[0].get("natIP")
                        if nat_ip:
                            return nat_ip
                    network_ip = iface.get("networkIP")
                    if network_ip:
                        return network_ip

    return None


def _parse_asset_to_node(asset, project_id):
    """Parse a single GCP asset into a normalized node dict.

    Args:
        asset: Raw asset dict from gcloud asset search-all-resources.
        project_id: The GCP project ID.

    Returns:
        Node dict or None if the asset type is not mapped.
    """
    asset_type = asset.get("assetType", "")
    resource_type, sub_type = map_gcp_resource(asset_type)

    if resource_type is None:
        return None

    name_path = asset.get("name", "")
    resource_name = _extract_name(name_path)
    display_name = asset.get("displayName", "") or resource_name
    region, zone = _extract_region_zone(asset)
    vpc_id = _extract_vpc_id(asset)
    endpoint = _extract_endpoint(asset)

    # Build metadata from useful asset fields
    metadata = {}
    state = asset.get("state", "")
    if state:
        metadata["state"] = state
    labels = asset.get("labels", {})
    if labels:
        metadata["labels"] = labels
    description = asset.get("description", "")
    if description:
        metadata["description"] = description
    create_time = asset.get("createTime", "")
    if create_time:
        metadata["create_time"] = create_time
    update_time = asset.get("updateTime", "")
    if update_time:
        metadata["update_time"] = update_time
    if asset_type:
        metadata["gcp_asset_type"] = asset_type

    return {
        "name": resource_name,
        "display_name": display_name,
        "resource_type": resource_type,
        "sub_type": sub_type,
        "provider": "gcp",
        "region": region,
        "zone": zone,
        "cloud_resource_id": name_path,
        "endpoint": endpoint,
        "vpc_id": vpc_id,
        "metadata": metadata,
    }


def _parse_relationships(raw_relationships, nodes_by_id):
    """Parse GCP Asset Inventory relationship data into dependency edges.

    Args:
        raw_relationships: List of relationship asset dicts from gcloud asset list.
        nodes_by_id: Dict mapping cloud_resource_id -> node name for resolving refs.

    Returns:
        List of relationship edge dicts.
    """
    edges = []

    for asset in raw_relationships:
        try:
            related_assets = asset.get("relatedAssets", {})
            if not related_assets:
                continue

            relationship_type = related_assets.get("relationshipAttributes", {}).get("type", "")
            dependency_type = GCP_RELATIONSHIP_TYPE_MAP.get(relationship_type, "network")

            source_name_path = asset.get("name", "")
            source_name = _resolve_node_name(source_name_path, nodes_by_id)
            if not source_name:
                continue

            assets_list = related_assets.get("assets", [])
            for related in assets_list:
                target_path = related.get("asset", "")
                target_name = _resolve_node_name(target_path, nodes_by_id)
                if not target_name or target_name == source_name:
                    continue

                edges.append({
                    "from_service": source_name,
                    "to_service": target_name,
                    "dependency_type": dependency_type,
                    "confidence": 1.0,
                    "discovered_from": ["gcp_asset_api"],
                })
        except Exception as e:
            logger.warning(f"Failed to parse relationship asset: {e}")
            continue

    return edges


def _resolve_node_name(resource_path, nodes_by_id):
    """Resolve a GCP resource path to a node name.

    First checks the nodes_by_id lookup (for exact matches), then falls back
    to extracting the last path segment.
    """
    if not resource_path:
        return None

    # Try direct lookup
    if resource_path in nodes_by_id:
        return nodes_by_id[resource_path]

    # Try stripping the //service.googleapis.com/ prefix for asset names
    for key, name in nodes_by_id.items():
        if key.endswith(resource_path) or resource_path.endswith(key):
            return name

    # Fall back to last path segment
    return _extract_name(resource_path)


def _build_gcloud_env(credentials):
    """Build environment arguments for gcloud commands based on credentials.

    Returns:
        List of extra gcloud arguments for authentication.
    """
    args = []
    service_account_key_path = credentials.get("service_account_key_path")
    if service_account_key_path:
        args.extend(["--impersonate-service-account", service_account_key_path])
    return args


def discover(user_id, credentials, env=None):
    """Discover all GCP resources using Cloud Asset Inventory API.

    Args:
        user_id: The Aurora user ID performing the discovery.
        credentials: Dict with at least 'project_id', and optionally
                     'service_account_key_path' for authentication.
        env: Optional environment dict for subprocess calls (for authentication).

    Returns:
        DiscoveryResult dict with keys:
            - nodes: List of normalized service node dicts.
            - relationships: List of dependency edge dicts.
            - errors: List of error message strings.
    """
    project_id = credentials.get("project_id")
    if not project_id:
        return {
            "nodes": [],
            "relationships": [],
            "errors": ["Missing required credential: project_id"],
        }

    logger.info(f"Starting GCP Asset Inventory discovery for project '{project_id}' (user: {user_id})")

    nodes = []
    relationships = []
    errors = []
    auth_args = _build_gcloud_env(credentials)

    # -----------------------------------------------------------------
    # Step 1: Discover all resources
    # -----------------------------------------------------------------
    try:
        resources_cmd = [
            "gcloud", "asset", "search-all-resources",
            f"--scope=projects/{project_id}",
            "--format=json",
        ] + auth_args

        raw_resources = _run_command(resources_cmd, env=env)

        if raw_resources is None:
            errors.append("Failed to fetch resources from Cloud Asset API")
        elif isinstance(raw_resources, list):
            logger.info(f"Fetched {len(raw_resources)} raw resources from GCP Asset Inventory")
            for asset in raw_resources:
                try:
                    node = _parse_asset_to_node(asset, project_id)
                    if node:
                        nodes.append(node)
                except Exception as e:
                    logger.warning(f"Failed to parse resource asset: {e}")
                    continue

    except RuntimeError as e:
        # Cloud Asset API not enabled
        return {
            "nodes": [],
            "relationships": [],
            "errors": [str(e)],
        }

    logger.info(f"Parsed {len(nodes)} recognized service nodes from {len(raw_resources or [])} assets")

    # Build lookup for relationship resolution
    nodes_by_id = {}
    for node in nodes:
        cloud_id = node.get("cloud_resource_id", "")
        if cloud_id:
            nodes_by_id[cloud_id] = node["name"]

    # -----------------------------------------------------------------
    # Step 2: Fetch IAM policies (for metadata enrichment)
    # -----------------------------------------------------------------
    try:
        iam_cmd = [
            "gcloud", "asset", "search-all-iam-policies",
            f"--scope=projects/{project_id}",
            "--format=json",
        ] + auth_args

        raw_iam = _run_command(iam_cmd, env=env)

        if raw_iam is None:
            errors.append("Failed to fetch IAM policies from Cloud Asset API")
        elif isinstance(raw_iam, list):
            logger.info(f"Fetched {len(raw_iam)} IAM policy bindings")
            # Enrich nodes with IAM binding counts
            iam_counts = {}
            for policy in raw_iam:
                resource = policy.get("resource", "")
                bindings = policy.get("policy", {}).get("bindings", [])
                resource_name = _extract_name(resource)
                if resource_name:
                    iam_counts[resource_name] = iam_counts.get(resource_name, 0) + len(bindings)

            for node in nodes:
                binding_count = iam_counts.get(node["name"], 0)
                if binding_count > 0:
                    node["metadata"]["iam_binding_count"] = binding_count

    except RuntimeError as e:
        errors.append(f"IAM policy fetch failed: {e}")

    # -----------------------------------------------------------------
    # Step 3: Fetch relationships via Cloud Asset relationship API
    # -----------------------------------------------------------------
    try:
        rel_cmd = [
            "gcloud", "asset", "list",
            f"--project={project_id}",
            "--content-type=relationship",
            "--format=json",
        ] + auth_args

        raw_relationships = _run_command(rel_cmd, env=env)

        if raw_relationships is None:
            errors.append("Failed to fetch relationships from Cloud Asset API")
        elif isinstance(raw_relationships, list):
            logger.info(f"Fetched {len(raw_relationships)} relationship assets")
            relationships = _parse_relationships(raw_relationships, nodes_by_id)

    except RuntimeError as e:
        errors.append(f"Relationship fetch failed: {e}")

    logger.info(
        f"GCP Asset discovery complete for project '{project_id}': "
        f"{len(nodes)} nodes, {len(relationships)} relationships, {len(errors)} errors"
    )

    return {
        "nodes": nodes,
        "relationships": relationships,
        "errors": errors,
    }
