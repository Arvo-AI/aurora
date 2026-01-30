"""
GCP Relationship API Inference - Phase 3 connection inference.

Processes ground-truth relationships from the GCP Cloud Asset API that were
collected during Phase 1 discovery. These relationships represent explicit
bindings declared in GCP (e.g. a Cloud SQL instance attached to a VPC, a
Cloud Function triggered by a Pub/Sub topic) and are therefore assigned
maximum confidence.

The raw relationship data is expected under enrichment_data["gcp_relationships"],
which is the list returned by GCP's ``gcloud asset list --content-type=relationship``
command.
"""

import logging

from services.discovery.resource_mapper import GCP_RELATIONSHIP_TYPE_MAP

logger = logging.getLogger(__name__)


def _build_node_lookup(graph_nodes):
    """Build a lookup dict mapping cloud_resource_id -> node name.

    Enables O(1) resolution from a GCP asset path to the corresponding
    node name in the graph.

    Returns:
        Dict mapping cloud_resource_id (str) -> node name (str).
    """
    lookup = {}
    for node in graph_nodes:
        resource_id = node.get("cloud_resource_id")
        name = node.get("name")
        if resource_id and name:
            lookup[resource_id] = name
    return lookup


def _resolve_node_name(resource_path, nodes_by_id):
    """Resolve a GCP resource path to a node name.

    Tries an exact match first, then checks for suffix/prefix overlaps
    (GCP asset names can include ``//service.googleapis.com/`` prefixes),
    and finally falls back to extracting the last path segment.

    Args:
        resource_path: Full or partial GCP asset resource path.
        nodes_by_id: Dict mapping cloud_resource_id -> node name.

    Returns:
        Node name string, or None if resolution fails.
    """
    if not resource_path:
        return None

    # Exact match
    if resource_path in nodes_by_id:
        return nodes_by_id[resource_path]

    # Suffix / prefix overlap (handles varying prefix formats)
    for key, name in nodes_by_id.items():
        if key.endswith(resource_path) or resource_path.endswith(key):
            return name

    # Last path segment as fallback
    segments = resource_path.rstrip("/").split("/")
    last_segment = segments[-1] if segments else None
    if last_segment and last_segment in nodes_by_id:
        return nodes_by_id[last_segment]

    return None


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from GCP Cloud Asset API relationship data.

    Args:
        user_id: The Aurora user ID performing inference.
        graph_nodes: List of service node dicts from Phase 1+2. Each node
            should have at minimum ``name`` and ``cloud_resource_id``.
        enrichment_data: Dict of enrichment results. This module reads
            ``enrichment_data["gcp_relationships"]`` -- a list of raw
            relationship asset dicts as returned by the GCP Cloud Asset API.

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
    raw_relationships = enrichment_data.get("gcp_relationships", [])
    if not raw_relationships:
        logger.debug("No GCP relationship data found in enrichment_data for user %s", user_id)
        return []

    nodes_by_id = _build_node_lookup(graph_nodes)
    if not nodes_by_id:
        logger.warning("No graph nodes with cloud_resource_id available for GCP relationship inference")
        return []

    edges = []
    seen = set()

    for asset in raw_relationships:
        try:
            related_assets = asset.get("relatedAssets", {})
            if not related_assets:
                continue

            relationship_type = (
                related_assets
                .get("relationshipAttributes", {})
                .get("type", "")
            )
            dependency_type = GCP_RELATIONSHIP_TYPE_MAP.get(relationship_type, "network")

            source_path = asset.get("name", "")
            source_name = _resolve_node_name(source_path, nodes_by_id)
            if not source_name:
                continue

            assets_list = related_assets.get("assets", [])
            for related in assets_list:
                target_path = related.get("asset", "")
                target_name = _resolve_node_name(target_path, nodes_by_id)
                if not target_name or target_name == source_name:
                    continue

                # Deduplicate edges
                edge_key = (source_name, target_name, dependency_type)
                if edge_key in seen:
                    continue
                seen.add(edge_key)

                edges.append({
                    "from_service": source_name,
                    "to_service": target_name,
                    "dependency_type": dependency_type,
                    "confidence": 1.0,
                    "discovered_from": ["gcp_asset_api"],
                })

        except Exception as e:
            logger.warning("Failed to parse GCP relationship asset: %s", e)
            continue

    logger.info(
        "GCP relationship inference complete for user %s: %d edges from %d raw relationships",
        user_id, len(edges), len(raw_relationships),
    )

    return edges
