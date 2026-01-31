"""
AWS CloudMap Inference - Phase 3 connection inference.

Processes AWS Cloud Map service discovery data to determine service-to-service
dependencies. Cloud Map provides explicit service registration, making this
one of the highest-confidence inference methods.
"""

import logging

logger = logging.getLogger(__name__)


def _build_ip_index(graph_nodes):
    """Build an index mapping IP addresses to graph node names.

    Scans node properties for private/public IP addresses and maps
    them to the owning node. This enables matching CloudMap instance
    IPs to discovered compute resources.

    Returns:
        Dict mapping IP address string to node name.
    """
    ip_index = {}
    for node in graph_nodes:
        node_name = node.get("name", "")
        if not node_name:
            continue

        # Check common IP-holding properties
        for key in ("private_ip", "public_ip", "ip_address", "private_ip_address",
                     "public_ip_address", "host_ip"):
            ip = node.get(key)
            if ip and isinstance(ip, str):
                ip_index[ip] = node_name

        # Check lists of IPs (e.g. network interfaces)
        for key in ("private_ips", "public_ips", "ip_addresses"):
            ips = node.get(key)
            if isinstance(ips, list):
                for ip in ips:
                    if ip and isinstance(ip, str):
                        ip_index[ip] = node_name

        # Check nested network interfaces
        interfaces = node.get("network_interfaces", [])
        if isinstance(interfaces, list):
            for iface in interfaces:
                if not isinstance(iface, dict):
                    continue
                for key in ("private_ip", "public_ip", "ip_address"):
                    ip = iface.get(key)
                    if ip and isinstance(ip, str):
                        ip_index[ip] = node_name

    return ip_index


def _find_node_by_resource_id(graph_nodes, resource_id):
    """Find a graph node by its resource ID (instance ID, task ARN, etc.).

    CloudMap instances often have AWS_INSTANCE_ID or similar attributes
    that map to EC2 instance IDs, ECS task IDs, or EKS pod IPs.
    """
    if not resource_id:
        return None
    resource_id_lower = resource_id.lower()
    for node in graph_nodes:
        node_id = (node.get("resource_id") or "").lower()
        node_arn = (node.get("arn") or "").lower()
        node_name = (node.get("name") or "").lower()
        if resource_id_lower in (node_id, node_arn, node_name):
            return node
    return None


def _resolve_instance_to_node(instance, graph_nodes, ip_index):
    """Resolve a CloudMap instance to a graph node.

    CloudMap instances carry attributes that can be matched:
    - AWS_INSTANCE_IPV4: IP address of the registered instance
    - AWS_INSTANCE_PORT: Port the service listens on
    - AWS_INSTANCE_ID: EC2 instance ID (for EC2-based services)
    - AWS_ALIAS_DNS_NAME: DNS alias for the service

    Args:
        instance: CloudMap instance dict with Id and Attributes.
        graph_nodes: List of graph node dicts.
        ip_index: Dict mapping IP addresses to node names.

    Returns:
        Node name string, or None if no match found.
    """
    attributes = instance.get("Attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}

    # Try IP-based matching first (most common for ECS/EKS)
    ipv4 = attributes.get("AWS_INSTANCE_IPV4", "")
    if ipv4 and ipv4 in ip_index:
        return ip_index[ipv4]

    # Try EC2 instance ID matching
    ec2_id = attributes.get("AWS_INSTANCE_ID", "")
    if ec2_id:
        node = _find_node_by_resource_id(graph_nodes, ec2_id)
        if node:
            return node.get("name")

    # Try the instance ID itself (could be a task ARN or custom ID)
    instance_id = instance.get("Id", "")
    if instance_id:
        node = _find_node_by_resource_id(graph_nodes, instance_id)
        if node:
            return node.get("name")

    return None


def _build_namespace_index(cloudmap_data):
    """Build a lookup from namespace ID to namespace name/DNS.

    Returns:
        Dict mapping namespace_id to namespace info dict.
    """
    index = {}
    namespaces = cloudmap_data.get("namespaces", [])
    for ns in namespaces:
        ns_id = ns.get("Id", "")
        if ns_id:
            index[ns_id] = {
                "name": ns.get("Name", ""),
                "type": ns.get("Type", ""),
            }
    return index


def infer(user_id, graph_nodes, enrichment_data):
    """Run AWS CloudMap inference.

    Processes CloudMap service discovery data to build service-to-service
    dependency edges. Services registered in CloudMap are matched to
    discovered graph nodes via IP addresses, instance IDs, or ARNs.

    Services that query a CloudMap DNS name (the consumer) depend on
    the registered instances (the providers), creating a consumer -> provider
    edge for each resolved instance.

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of discovered graph node dicts.
        enrichment_data: Dict of enrichment data from Phase 2.

    Returns:
        List of dependency edge dicts with confidence 0.95.
    """
    edges = []
    cloudmap_data = enrichment_data.get("cloudmap_services", {})

    if not cloudmap_data:
        logger.info("CloudMap inference for user %s: no CloudMap data available", user_id)
        return edges

    services = cloudmap_data.get("services", [])
    if not services:
        logger.info("CloudMap inference for user %s: no CloudMap services found", user_id)
        return edges

    ip_index = _build_ip_index(graph_nodes)
    namespace_index = _build_namespace_index(cloudmap_data)

    for cm_service in services:
        service_name = cm_service.get("service_name", "")
        namespace_id = cm_service.get("namespace_id", "")
        instances = cm_service.get("instances", [])

        namespace_info = namespace_index.get(namespace_id, {})
        namespace_name = namespace_info.get("name", "")

        # Build the DNS name that consumers would query
        dns_name = f"{service_name}.{namespace_name}" if namespace_name else service_name

        # Resolve each registered instance to a graph node
        resolved_providers = []
        for instance in instances:
            provider_name = _resolve_instance_to_node(instance, graph_nodes, ip_index)
            if provider_name:
                resolved_providers.append(provider_name)

        if not resolved_providers:
            logger.debug(
                "CloudMap service %s has %d instances but none resolved to graph nodes",
                service_name, len(instances),
            )
            continue

        # Find nodes that reference this CloudMap service via DNS or service name.
        # Any compute node whose env vars or config reference the DNS name is a consumer.
        env_vars = enrichment_data.get("env_vars", {})
        consumers = set()

        for node_name, variables in env_vars.items():
            if not isinstance(variables, dict):
                continue
            for var_name, var_value in variables.items():
                if not isinstance(var_value, str):
                    continue
                if dns_name in var_value or service_name in var_value:
                    consumers.add(node_name)

        # Also check DNS records for references
        dns_records = enrichment_data.get("dns_records", [])
        for record in dns_records:
            record_name = record.get("Name", "")
            if dns_name in record_name:
                # Any node referencing this DNS record is a consumer
                for node_name, variables in env_vars.items():
                    if not isinstance(variables, dict):
                        continue
                    for var_value in variables.values():
                        if isinstance(var_value, str) and record_name in var_value:
                            consumers.add(node_name)

        # Create edges: consumer -> each provider
        for consumer in consumers:
            for provider in resolved_providers:
                if consumer == provider:
                    continue  # Skip self-references
                edges.append({
                    "from_service": consumer,
                    "to_service": provider,
                    "dependency_type": "http",
                    "confidence": 0.95,
                    "discovered_from": ["cloudmap"],
                    "detail": f"CloudMap service {dns_name} (consumer {consumer} -> provider {provider})",
                })

        # If no explicit consumers found but providers are resolved,
        # create edges between the CloudMap service node itself and providers
        if not consumers:
            # Check if the CloudMap namespace itself is a node
            cm_node_name = None
            for node in graph_nodes:
                if node.get("sub_type") == "cloudmap" and (
                    node.get("name", "") == namespace_name
                    or service_name in (node.get("name") or "")
                ):
                    cm_node_name = node.get("name")
                    break

            if cm_node_name:
                for provider in resolved_providers:
                    if cm_node_name == provider:
                        continue
                    edges.append({
                        "from_service": cm_node_name,
                        "to_service": provider,
                        "dependency_type": "http",
                        "confidence": 0.95,
                        "discovered_from": ["cloudmap"],
                        "detail": f"CloudMap service {dns_name} registered provider {provider}",
                    })

    logger.info(
        "CloudMap inference for user %s: %d edges from %d CloudMap services",
        user_id, len(edges), len(services),
    )

    return edges
