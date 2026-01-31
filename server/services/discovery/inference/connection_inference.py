"""
Connection Inference Orchestrator - Phase 3 entry point.

Runs all 11 inference methods, collects edges, deduplicates by keeping the
highest confidence per (from_service, to_service) pair, and merges
discovered_from lists.
"""

import logging

from services.discovery.inference import (
    gcp_relationship_inference,
    security_group_inference,
    load_balancer_inference,
    env_var_inference,
    dns_inference,
    event_source_inference,
    iam_inference,
    storage_inference,
    secret_store_inference,
    cloudmap_inference,
    network_proximity_inference,
)

logger = logging.getLogger(__name__)

# Ordered list of (name, module) for all inference methods.
_INFERENCE_MODULES = [
    ("gcp_relationship", gcp_relationship_inference),
    ("security_group", security_group_inference),
    ("load_balancer", load_balancer_inference),
    ("env_var", env_var_inference),
    ("dns", dns_inference),
    ("event_source", event_source_inference),
    ("iam", iam_inference),
    ("storage", storage_inference),
    ("secret_store", secret_store_inference),
    ("cloudmap", cloudmap_inference),
    ("network_proximity", network_proximity_inference),
]


def _deduplicate_edges(edges):
    """Deduplicate edges by (from_service, to_service) pair.

    For duplicate edges (same source and target), keeps the entry with the
    highest confidence score and merges all discovered_from sources.

    Args:
        edges: List of edge dicts with keys: from_service, to_service,
               dependency_type, confidence, discovered_from.

    Returns:
        List of deduplicated edge dicts.
    """
    edge_map = {}

    for edge in edges:
        key = (edge["from_service"], edge["to_service"])

        if key not in edge_map:
            # First occurrence — clone to avoid mutating the original
            edge_map[key] = {
                "from_service": edge["from_service"],
                "to_service": edge["to_service"],
                "dependency_type": edge.get("dependency_type", "unknown"),
                "confidence": edge.get("confidence", 0.5),
                "discovered_from": list(edge.get("discovered_from", [])),
            }
            if "detail" in edge:
                edge_map[key]["detail"] = edge["detail"]
        else:
            existing = edge_map[key]

            # Keep the higher confidence
            if edge.get("confidence", 0.5) > existing["confidence"]:
                existing["confidence"] = edge["confidence"]
                existing["dependency_type"] = edge.get("dependency_type", existing["dependency_type"])
                if "detail" in edge:
                    existing["detail"] = edge["detail"]

            # Merge discovered_from lists (deduplicated)
            for source in edge.get("discovered_from", []):
                if source not in existing["discovered_from"]:
                    existing["discovered_from"].append(source)

    return list(edge_map.values())


def run_all_inference(user_id, graph_nodes, enrichment_data):
    """Run all 11 inference methods and deduplicate edges.

    Each inference module's infer() function is called independently.
    If one module fails, the others still run. Results are collected,
    deduplicated by (from_service, to_service) pair, and returned.

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of discovered graph node dicts from Phase 1.
        enrichment_data: Dict of enrichment data from Phase 2.

    Returns:
        List of deduplicated dependency edge dicts with highest confidence
        per edge pair.
    """
    all_edges = []
    summary = []

    for name, module in _INFERENCE_MODULES:
        try:
            edges = module.infer(user_id, graph_nodes, enrichment_data)
            edge_count = len(edges) if edges else 0
            all_edges.extend(edges or [])
            summary.append(f"{name}={edge_count}")
            logger.info("Inference [%s]: produced %d edges", name, edge_count)
        except Exception:
            logger.exception("Inference [%s] failed", name)
            summary.append(f"{name}=ERROR")

    # Deduplicate
    deduplicated = _deduplicate_edges(all_edges)

    logger.info(
        "Connection inference complete for user %s: %d raw edges -> %d deduplicated. "
        "Breakdown: %s",
        user_id,
        len(all_edges),
        len(deduplicated),
        ", ".join(summary),
    )

    return deduplicated
