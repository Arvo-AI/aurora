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


def _run_command(args, env=None, progress_timeout=30):
    """Run gcloud command and fail fast if frozen (no output) vs slow (making progress).

    Args:
        args: Command arguments (e.g. ["gcloud", "asset", ...]).
        env: Optional environment dict for subprocess.
        progress_timeout: Seconds without any output before killing process (default 30s).

    Returns:
        Parsed JSON output, or None on failure.

    Raises:
        RuntimeError: If Cloud Asset API is not enabled.
    """
    import threading
    import time
    import queue

    output_queue = queue.Queue()
    last_activity = {'time': time.time()}

    def stream_reader(pipe, pipe_name):
        """Read pipe and track activity."""
        try:
            for line in iter(pipe.readline, ''):
                if line:
                    last_activity['time'] = time.time()
                    output_queue.put((pipe_name, line))
        finally:
            pipe.close()

    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, bufsize=1
        )

        # Start threads to read stdout/stderr without blocking
        stdout_thread = threading.Thread(target=stream_reader, args=(process.stdout, 'out'))
        stderr_thread = threading.Thread(target=stream_reader, args=(process.stderr, 'err'))
        stdout_thread.daemon = True
        stderr_thread.daemon = True
        stdout_thread.start()
        stderr_thread.start()

        stdout_lines, stderr_lines = [], []

        # Monitor for progress until process completes
        while process.poll() is None:
            if time.time() - last_activity['time'] > progress_timeout:
                process.kill()
                logger.error(f"gcloud frozen (no output for {progress_timeout}s): {' '.join(args)}")
                return None

            try:
                pipe_name, line = output_queue.get(timeout=1)
                (stdout_lines if pipe_name == 'out' else stderr_lines).append(line)
            except queue.Empty:
                continue

        # Collect any remaining output
        while not output_queue.empty():
            pipe_name, line = output_queue.get_nowait()
            (stdout_lines if pipe_name == 'out' else stderr_lines).append(line)

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        stdout = ''.join(stdout_lines).strip()
        stderr = ''.join(stderr_lines).strip()

        if process.returncode != 0:
            if "Cloud Asset API" in stderr and ("not been used" in stderr or "not enabled" in stderr):
                raise RuntimeError(
                    "Cloud Asset API not enabled. Enable: gcloud services enable cloudasset.googleapis.com"
                )
            logger.error(f"gcloud failed (rc={process.returncode}): {stderr}")
            return None

        return json.loads(stdout) if stdout else []

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse gcloud JSON: {e}")
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


def _extract_vpc_id(asset, project_id=None):
    """Extract VPC network name from a GCP Cloud Asset Inventory asset.

    The search-all-resources API returns a flat format (no nested
    resource.data), so we extract network info from:
      1. additionalAttributes (network, networkRef, subnetwork fields)
      2. The asset name itself if it's a VPC network resource
      3. Fall back to project ID as a grouping key — most GCP resources
         within a project can communicate via the default VPC, so
         project-level grouping is a reasonable proxy.

    Args:
        asset: Raw asset dict from gcloud asset search-all-resources.
        project_id: The GCP project ID (used as fallback grouping key).

    Returns:
        VPC network name string, or project-based fallback, or None.
    """
    # Helper: build the project-prefixed VPC grouping key.
    # Always includes the project so that resources sharing a project are
    # grouped together for network proximity inference, even if some have
    # explicit VPC refs and others don't.
    def _vpc_key(network_name=None):
        if project_id and network_name:
            return f"gcp-{project_id}/{network_name}"
        if project_id:
            return f"gcp-{project_id}"
        if network_name:
            return network_name
        return None

    # 1. Try additionalAttributes (present on many compute/networking resources)
    additional = asset.get("additionalAttributes", {})
    if isinstance(additional, dict):
        for key in ("network", "networkRef", "networkUri", "vpcNetwork",
                     "subnetwork", "subnetworkRef"):
            val = additional.get(key)
            if val and isinstance(val, str):
                if "/networks/" in val:
                    parts = val.split("/networks/")
                    if len(parts) == 2:
                        return _vpc_key(parts[1].split("/")[0])
                return _vpc_key(_extract_name(val))

    # 2. If this asset IS a VPC network, extract its name from the asset path
    name_path = asset.get("name", "")
    asset_type = asset.get("assetType", "")
    if asset_type == "compute.googleapis.com/Network" and name_path:
        return _vpc_key(_extract_name(name_path))

    # 3. Fall back to project ID as a VPC grouping key.
    #    In GCP, resources within the same project typically share the default
    #    VPC network and can communicate internally.
    return _vpc_key()


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
    vpc_id = _extract_vpc_id(asset, project_id=project_id)
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


def _discover_project(project_id, auth_args, env=None):
    """Discover resources, IAM policies, and relationships for a single GCP project.

    Returns:
        (nodes_list, relationships_list, errors_list)
    """
    nodes = []
    errors = []
    raw_resources = None

    # Step 1: Resources
    try:
        resources_cmd = [
            "gcloud", "asset", "search-all-resources",
            f"--scope=projects/{project_id}",
            "--format=json",
        ] + auth_args

        raw_resources = _run_command(resources_cmd, env=env)

        if raw_resources is None:
            errors.append(f"[{project_id}] Failed to fetch resources from Cloud Asset API")
        elif isinstance(raw_resources, list):
            logger.info(f"[{project_id}] Fetched {len(raw_resources)} raw resources")
            for asset in raw_resources:
                try:
                    node = _parse_asset_to_node(asset, project_id)
                    if node:
                        nodes.append(node)
                except Exception as e:
                    logger.warning(f"[{project_id}] Failed to parse resource asset: {e}")
    except RuntimeError as e:
        errors.append(f"[{project_id}] {e}")

    logger.info(f"[{project_id}] Parsed {len(nodes)} nodes from {len(raw_resources or [])} assets")

    # Build lookup for relationship resolution
    nodes_by_id = {}
    for node in nodes:
        cloud_id = node.get("cloud_resource_id", "")
        if cloud_id:
            nodes_by_id[cloud_id] = node["name"]

    # Step 2: IAM policies
    try:
        iam_cmd = [
            "gcloud", "asset", "search-all-iam-policies",
            f"--scope=projects/{project_id}",
            "--format=json",
        ] + auth_args

        raw_iam = _run_command(iam_cmd, env=env)

        if raw_iam is None:
            errors.append(f"[{project_id}] Failed to fetch IAM policies")
        elif isinstance(raw_iam, list):
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
        errors.append(f"[{project_id}] IAM policy fetch failed: {e}")

    # Step 3: Relationships
    relationships = []
    try:
        rel_cmd = [
            "gcloud", "asset", "list",
            f"--project={project_id}",
            "--content-type=relationship",
            "--format=json",
        ] + auth_args

        raw_relationships = _run_command(rel_cmd, env=env)

        if raw_relationships is None:
            errors.append(f"[{project_id}] Failed to fetch relationships")
        elif isinstance(raw_relationships, list):
            logger.info(f"[{project_id}] Fetched {len(raw_relationships)} relationship assets")
            relationships = _parse_relationships(raw_relationships, nodes_by_id)
    except RuntimeError as e:
        errors.append(f"[{project_id}] Relationship fetch failed: {e}")

    return nodes, relationships, errors


def discover(user_id, credentials, env=None):
    """Discover all GCP resources using Cloud Asset Inventory API.

    Args:
        user_id: The Aurora user ID performing the discovery.
        credentials: Dict with either:
            - project_ids: List of project IDs to scan (preferred).
            - project_id: Single project ID (legacy fallback).
        env: Optional environment dict for subprocess calls (for authentication).

    Returns:
        DiscoveryResult dict with keys:
            - nodes: List of normalized service node dicts.
            - relationships: List of dependency edge dicts.
            - errors: List of error message strings.
    """
    # Support both single project_id and multi-project project_ids
    project_ids = credentials.get("project_ids") or []
    if not project_ids:
        single = credentials.get("project_id")
        if single:
            project_ids = [single]

    if not project_ids:
        return {
            "nodes": [],
            "relationships": [],
            "errors": ["Missing required credential: project_id or project_ids"],
        }

    logger.info(
        f"Starting GCP Asset Inventory discovery for {len(project_ids)} project(s) "
        f"(user: {user_id}): {project_ids}"
    )

    all_nodes = []
    all_relationships = []
    all_errors = []
    auth_args = _build_gcloud_env(credentials)

    for project_id in project_ids:
        logger.info(f"Discovering project '{project_id}'...")
        nodes, relationships, errors = _discover_project(project_id, auth_args, env=env)
        all_nodes.extend(nodes)
        all_relationships.extend(relationships)
        all_errors.extend(errors)
        logger.info(
            f"Project '{project_id}': {len(nodes)} nodes, "
            f"{len(relationships)} relationships, {len(errors)} errors"
        )

    logger.info(
        f"GCP Asset discovery complete for {len(project_ids)} project(s): "
        f"{len(all_nodes)} nodes, {len(all_relationships)} relationships, {len(all_errors)} errors"
    )

    return {
        "nodes": all_nodes,
        "relationships": all_relationships,
        "errors": all_errors,
    }
