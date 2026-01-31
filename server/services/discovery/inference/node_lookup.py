"""
Shared node lookup utilities for Phase 3 inference modules.

Provides common functions for resolving AWS ARNs, node names, and endpoints
to graph node names. Used by multiple inference engines to avoid duplication.
"""


def extract_name_from_arn(arn):
    """Extract the resource name from an AWS ARN.

    ARN format: arn:aws:service:region:account:resource-type/resource-name
    or           arn:aws:service:region:account:resource-name

    Returns the last meaningful segment, or None if the ARN is invalid.
    """
    if not arn:
        return None
    parts = arn.split(":")
    if len(parts) < 6:
        return None
    resource_part = parts[-1]
    if "/" in resource_part:
        return resource_part.split("/")[-1]
    return resource_part


def find_node_by_name(name, graph_nodes):
    """Find a graph node whose name matches (case-insensitive).

    Args:
        name: The name to search for.
        graph_nodes: List of graph node dicts.

    Returns:
        The node's canonical name string, or None if not found.
    """
    if not name:
        return None
    name_lower = name.lower()
    for node in graph_nodes:
        if (node.get("name") or "").lower() == name_lower:
            return node["name"]
    return None


def find_node_by_arn(arn, graph_nodes):
    """Find a graph node whose cloud_resource_id matches the given ARN.

    Tries an exact match on cloud_resource_id first, then falls back
    to matching by the resource name extracted from the ARN.

    Args:
        arn: AWS ARN string to match.
        graph_nodes: List of graph node dicts.

    Returns:
        The node's canonical name string, or None if not found.
    """
    if not arn:
        return None
    arn_lower = arn.lower()
    for node in graph_nodes:
        cloud_id = (node.get("cloud_resource_id") or "").lower()
        if cloud_id and cloud_id == arn_lower:
            return node["name"]
    return find_node_by_name(extract_name_from_arn(arn), graph_nodes)


def find_compute_node(graph_nodes, service_name):
    """Find a compute/serverless node by exact name match.

    Args:
        graph_nodes: List of graph node dicts.
        service_name: The exact node name to search for.

    Returns:
        The matching node dict, or None if not found.
    """
    for node in graph_nodes:
        if node.get("name") == service_name:
            return node
    return None


def find_node_by_endpoint(endpoint, graph_nodes):
    """Find a graph node whose endpoint matches (substring or exact).

    Args:
        endpoint: Endpoint URL or hostname to match.
        graph_nodes: List of graph node dicts.

    Returns:
        The node's canonical name string, or None if not found.
    """
    if not endpoint:
        return None
    endpoint_lower = endpoint.lower()
    for node in graph_nodes:
        node_endpoint = (node.get("endpoint") or "").lower()
        if node_endpoint and (node_endpoint == endpoint_lower or endpoint_lower in node_endpoint):
            return node["name"]
    return None
