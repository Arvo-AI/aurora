"""
Environment Variable Inference - Phase 3 connection inference.

Parses environment variable dependency hints extracted during Phase 2
serverless enrichment to discover DEPENDS_ON edges between services.
When a serverless function's environment variables reference a hostname
that matches a known graph node (by endpoint, cloud_resource_id, or
Kubernetes DNS name), an edge is inferred between the function and the
target service.

The enrichment data is expected under ``enrichment_data["env_vars"]``,
which is a dict mapping service name to a dict containing
``parsed_dependencies`` -- a list of parsed env var dependency hints
with ``hostname``, ``port``, ``type``, and ``env_key`` fields.

This module never creates placeholder nodes for unresolved hostnames.
If a hostname cannot be matched to an existing graph node, the
dependency hint is silently skipped.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Pattern to parse Kubernetes DNS names.
# Format: <service>.<namespace>.svc.cluster.local
# or shorter: <service>.<namespace>.svc
K8S_DNS_PATTERN = re.compile(
    r"^(?P<service>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)"
    r"\.(?P<namespace>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)"
    r"\.svc(?:\.cluster\.local)?$",
    re.IGNORECASE,
)


def _build_node_lookups(graph_nodes):
    """Build fast lookup structures for matching hostnames to graph nodes.

    Returns:
        Tuple of (by_endpoint, by_resource_id, by_name, k8s_nodes):
            by_endpoint: Dict mapping endpoint/hostname -> node name.
            by_resource_id: Dict mapping cloud_resource_id -> node name.
            by_name: Dict mapping lowercase node name -> node name.
            k8s_nodes: Dict mapping (lowercase_name, namespace) -> node name
                for Kubernetes service nodes.
    """
    by_endpoint = {}
    by_resource_id = {}
    by_name = {}
    k8s_nodes = {}

    for node in graph_nodes:
        name = node.get("name")
        if not name:
            continue

        # Index by name (case-insensitive)
        by_name[name.lower()] = name

        # Index by endpoint
        endpoint = node.get("endpoint", "")
        if endpoint:
            # Strip protocol prefix if present
            clean_endpoint = endpoint
            if "://" in clean_endpoint:
                clean_endpoint = clean_endpoint.split("://", 1)[1]
            # Strip trailing path
            clean_endpoint = clean_endpoint.split("/")[0]
            # Strip port
            clean_endpoint_no_port = clean_endpoint.rsplit(":", 1)[0] if ":" in clean_endpoint else clean_endpoint
            by_endpoint[clean_endpoint] = name
            by_endpoint[clean_endpoint_no_port] = name
            by_endpoint[endpoint] = name

        # Index by cloud_resource_id
        resource_id = node.get("cloud_resource_id", "")
        if resource_id:
            by_resource_id[resource_id] = name

        # Index Kubernetes nodes by (name, namespace)
        resource_type = node.get("resource_type", "")
        namespace = node.get("namespace", "")
        if resource_type in ("k8s_service", "k8s_deployment", "k8s_statefulset"):
            k8s_nodes[(name.lower(), namespace.lower() if namespace else "default")] = name

        # Also index by private/public IPs
        for ip_field in ("private_ip", "public_ip"):
            ip_val = node.get(ip_field, "")
            if ip_val:
                by_endpoint[ip_val] = name

    return by_endpoint, by_resource_id, by_name, k8s_nodes


def _resolve_hostname(hostname, by_endpoint, by_resource_id, by_name, k8s_nodes):
    """Resolve a hostname from an env var dependency to a graph node name.

    Resolution order:
        1. Direct endpoint match
        2. Cloud resource ID match
        3. Kubernetes DNS name parsing (service.namespace.svc.cluster.local)
        4. Lowercase node name match (fallback)

    Args:
        hostname: The hostname string to resolve.
        by_endpoint: Dict mapping endpoint -> node name.
        by_resource_id: Dict mapping cloud_resource_id -> node name.
        by_name: Dict mapping lowercase name -> node name.
        k8s_nodes: Dict mapping (name, namespace) -> node name.

    Returns:
        Node name string, or None if unresolved.
    """
    if not hostname:
        return None

    # 1. Direct endpoint match
    if hostname in by_endpoint:
        return by_endpoint[hostname]

    # Strip port if present (e.g. "redis:6379" -> "redis")
    hostname_no_port = hostname.rsplit(":", 1)[0] if ":" in hostname else hostname
    if hostname_no_port in by_endpoint:
        return by_endpoint[hostname_no_port]

    # 2. Cloud resource ID match
    if hostname in by_resource_id:
        return by_resource_id[hostname]

    # 3. Kubernetes DNS name parsing
    k8s_match = K8S_DNS_PATTERN.match(hostname)
    if k8s_match:
        svc_name = k8s_match.group("service").lower()
        namespace = k8s_match.group("namespace").lower()
        k8s_key = (svc_name, namespace)
        if k8s_key in k8s_nodes:
            return k8s_nodes[k8s_key]
        # Try matching just by service name across all namespaces
        for (node_name, ns), full_name in k8s_nodes.items():
            if node_name == svc_name:
                return full_name

    # 4. Lowercase node name match (for simple hostnames like "redis-master")
    hostname_lower = hostname_no_port.lower()
    if hostname_lower in by_name:
        return by_name[hostname_lower]

    # Try matching the first segment of the hostname (e.g. "db.internal.example.com" -> "db")
    first_segment = hostname_lower.split(".")[0]
    if first_segment in by_name:
        return by_name[first_segment]

    return None


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from environment variable dependency hints.

    Processes ``enrichment_data["env_vars"]`` -- a dict mapping service
    names to their parsed environment variable dependencies, as produced
    by Phase 2 serverless enrichment. Each dependency contains a
    ``hostname`` that is matched against known graph nodes.

    Only creates edges when both the source service and the target
    hostname can be resolved to existing graph nodes. Unresolved
    hostnames are skipped (no placeholder nodes are created during
    inference).

    Args:
        user_id: The Aurora user ID performing inference.
        graph_nodes: List of service node dicts from Phase 1+2.
        enrichment_data: Dict of enrichment results. Reads:
            - ``env_vars``: Dict mapping service name to
              ``{"parsed_dependencies": [{"hostname", "port", "type", "env_key"}]}``.

    Returns:
        List of dependency edge dicts::

            [{
                "from_service": str,
                "to_service": str,
                "dependency_type": str,
                "confidence": float,
                "discovered_from": [str],
            }]
    """
    env_vars_data = enrichment_data.get("env_vars", {})
    if not env_vars_data:
        logger.debug("No env var data found in enrichment_data for user %s", user_id)
        return []

    by_endpoint, by_resource_id, by_name, k8s_nodes = _build_node_lookups(graph_nodes)

    # Build a set of known source service names for validation
    known_services = {node.get("name") for node in graph_nodes if node.get("name")}

    edges = []
    seen = set()
    unresolved_count = 0

    for service_name, service_data in env_vars_data.items():
        # Verify the source service exists in the graph
        if service_name not in known_services:
            logger.debug(
                "Source service '%s' from env var data not found in graph nodes, skipping",
                service_name,
            )
            continue

        parsed_deps = service_data.get("parsed_dependencies", [])
        for dep in parsed_deps:
            hostname = dep.get("hostname")
            if not hostname:
                continue

            dep_type = dep.get("type", "network")

            # Resolve hostname to a graph node
            target_name = _resolve_hostname(
                hostname, by_endpoint, by_resource_id, by_name, k8s_nodes,
            )

            if not target_name:
                unresolved_count += 1
                logger.debug(
                    "Could not resolve hostname '%s' (env key: %s) from service '%s'",
                    hostname, dep.get("env_key", "unknown"), service_name,
                )
                continue

            if target_name == service_name:
                continue

            edge_key = (service_name, target_name, dep_type)
            if edge_key in seen:
                continue
            seen.add(edge_key)

            edges.append({
                "from_service": service_name,
                "to_service": target_name,
                "dependency_type": dep_type,
                "confidence": 0.7,
                "discovered_from": ["env_var"],
            })

    logger.info(
        "Env var inference complete for user %s: %d edges from %d services "
        "(%d hostnames unresolved)",
        user_id, len(edges), len(env_vars_data), unresolved_count,
    )

    return edges
