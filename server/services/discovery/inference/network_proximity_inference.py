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
_DEPENDENCY_PATTERNS = {
    ("vm", "database"),
    ("vm", "cache"),
    ("vm", "message_queue"),
    ("vm", "search_engine"),
    ("vm", "storage_bucket"),
    ("vm", "filesystem"),
    ("vm", "secret_store"),
    ("serverless_function", "database"),
    ("serverless_function", "cache"),
    ("serverless_function", "message_queue"),
    ("serverless_function", "search_engine"),
    ("serverless_function", "storage_bucket"),
    ("serverless_function", "secret_store"),
    ("kubernetes_cluster", "database"),
    ("kubernetes_cluster", "cache"),
    ("kubernetes_cluster", "message_queue"),
    ("kubernetes_cluster", "search_engine"),
    ("kubernetes_cluster", "storage_bucket"),
    ("kubernetes_cluster", "filesystem"),
    ("kubernetes_cluster", "secret_store"),
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
    # Group nodes by VPC
    vpc_groups = defaultdict(list)
    for node in graph_nodes:
        vpc_id = node.get("vpc_id")
        if vpc_id:
            vpc_groups[vpc_id].append(node)

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
