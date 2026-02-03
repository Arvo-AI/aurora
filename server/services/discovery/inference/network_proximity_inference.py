"""
Network Proximity (VPC/Subnet) - Phase 3 inference engine.

Infers weak DEPENDS_ON edges between resources that share the same VPC.
This is the weakest signal (confidence 0.5) because co-location in a VPC
only indicates network reachability, not actual dependency.

Heuristics:
  - Only creates edges between resources of *different* types that suggest
    a real dependency pattern (e.g. vm -> database, serverless -> cache).
  - Does NOT create edges between same-type resources (two VMs don't
    automatically depend on each other).
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# Resource types that act as consumers (clients).
_CONSUMER_TYPES = {"vm", "serverless_function", "kubernetes_cluster"}

# Resource types that act as backends (servers/dependencies).
_BACKEND_TYPES = {
    "database",
    "cache",
    "message_queue",
    "search_engine",
    "storage_bucket",
    "filesystem",
    "secret_store",
}

# Valid consumer -> backend dependency patterns.
# Only these combinations generate edges.
# Built from the product of consumer and backend types, minus excluded pairs.
_DEPENDENCY_PATTERNS = {
    (c, b) for c in _CONSUMER_TYPES for b in _BACKEND_TYPES
} - {
    ("serverless_function", "filesystem"),
}

# Map backend resource types to dependency type labels.
_BACKEND_TYPE_TO_DEPENDENCY = {
    "database": "database",
    "cache": "cache",
    "message_queue": "queue",
    "search_engine": "search",
    "storage_bucket": "storage",
    "filesystem": "storage",
    "secret_store": "secret_access",
}


def infer(user_id, graph_nodes, enrichment_data):
    """Infer weak DEPENDS_ON edges from VPC co-location.

    Groups service nodes by vpc_id, then within each VPC creates edges
    from consumer-type resources to backend-type resources. This captures
    the most likely dependency direction (compute -> data store).

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of service node dicts from Phase 1.
        enrichment_data: Dict from Phase 2 enrichment (unused by this
            engine but accepted for interface consistency).

    Returns:
        List of dependency edge dicts with keys: from_service, to_service,
        dependency_type, confidence, discovered_from.
    """
    # Group nodes by VPC.
    # For GCP, vpc_id may be "gcp-<project>/<vpc-name>" (resources with an
    # explicit VPC) or "gcp-<project>" (resources without a VPC, e.g. Pub/Sub,
    # Cloud Storage).  We group by the full vpc_id first, then merge all
    # groups that share the same GCP project prefix so that VMs in
    # "gcp-proj/default" can reach Pub/Sub topics in "gcp-proj".
    vpc_groups = defaultdict(list)
    for node in graph_nodes:
        vpc_id = node.get("vpc_id")
        if vpc_id:
            vpc_groups[vpc_id].append(node)

    # Merge GCP VPC groups that share the same project prefix.
    # "gcp-proj/default" and "gcp-proj" -> merged under "gcp-proj".
    gcp_project_groups = defaultdict(list)
    non_gcp_keys = []
    for vpc_id in list(vpc_groups.keys()):
        if vpc_id.startswith("gcp-"):
            # Extract the project part: "gcp-proj/vpc-name" -> "gcp-proj"
            project_key = vpc_id.split("/")[0]
            gcp_project_groups[project_key].append(vpc_id)
        else:
            non_gcp_keys.append(vpc_id)

    # Build the final merged groups
    merged_groups = {}
    for project_key, vpc_ids in gcp_project_groups.items():
        merged_nodes = []
        for vid in vpc_ids:
            merged_nodes.extend(vpc_groups[vid])
        merged_groups[project_key] = merged_nodes
    for vpc_id in non_gcp_keys:
        merged_groups[vpc_id] = vpc_groups[vpc_id]

    vpc_groups = merged_groups

    if not vpc_groups:
        logger.debug("No VPC-grouped nodes for user %s", user_id)
        return []

    edges = []
    seen = set()

    for vpc_id, nodes in vpc_groups.items():
        # Separate consumers and backends within this VPC
        consumers = [n for n in nodes if n.get("resource_type") in _CONSUMER_TYPES]
        backends = [n for n in nodes if n.get("resource_type") in _BACKEND_TYPES]

        if not consumers or not backends:
            continue

        for consumer in consumers:
            consumer_type = consumer.get("resource_type")
            consumer_name = consumer.get("name")
            if not consumer_name:
                continue

            for backend in backends:
                backend_type = backend.get("resource_type")
                backend_name = backend.get("name")
                if not backend_name:
                    continue

                # Only create edges for valid dependency patterns
                if (consumer_type, backend_type) not in _DEPENDENCY_PATTERNS:
                    continue

                edge_key = (consumer_name, backend_name)
                if edge_key in seen:
                    continue
                seen.add(edge_key)

                dep_type = _BACKEND_TYPE_TO_DEPENDENCY.get(backend_type, "network")

                edges.append({
                    "from_service": consumer_name,
                    "to_service": backend_name,
                    "dependency_type": dep_type,
                    "confidence": 0.5,
                    "discovered_from": ["network_topology"],
                })

    logger.info(
        "Network proximity inference for user %s: %d edges across %d VPCs",
        user_id, len(edges), len(vpc_groups),
    )
    return edges
