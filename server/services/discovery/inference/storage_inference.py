"""
Object Storage Inference - Phase 3 connection inference.

Determines which compute services access storage buckets (S3, GCS, Azure Blob)
by analyzing environment variables, IAM policies, and event source mappings.
"""

import logging
import re

from services.discovery.inference.node_lookup import find_compute_node

logger = logging.getLogger(__name__)

# Patterns that indicate an env var points to an S3 or GCS bucket
_BUCKET_ENV_PATTERNS = [
    re.compile(r"^s3://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])(?:/|$)", re.IGNORECASE),
    re.compile(r"^gs://([a-z0-9][a-z0-9.\-_]{1,220}[a-z0-9])(?:/|$)", re.IGNORECASE),
    re.compile(r"^https?://([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])\.s3[.\-]", re.IGNORECASE),
    re.compile(r"^https?://storage\.googleapis\.com/([a-z0-9][a-z0-9.\-_]{1,220}[a-z0-9])", re.IGNORECASE),
]

# IAM actions that indicate storage access
_S3_ACTIONS = {
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:*",
}

# S3 ARN pattern: arn:aws:s3:::bucket-name or arn:aws:s3:::bucket-name/*
_S3_ARN_PATTERN = re.compile(r"^arn:aws:s3:::([a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])")

# S3 event source ARN pattern for event trigger detection
_S3_EVENT_SOURCE_PATTERN = re.compile(r"^arn:aws:s3:::(.+)$")


def _extract_bucket_from_env_value(value):
    """Extract a bucket name from an environment variable value.

    Returns:
        Bucket name string, or None if the value does not reference a bucket.
    """
    if not value or not isinstance(value, str):
        return None
    for pattern in _BUCKET_ENV_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def _find_bucket_node(graph_nodes, bucket_name):
    """Find a storage bucket node in the graph by its name.

    Checks both the node name and any ARN/URI properties for a match.
    """
    bucket_name_lower = bucket_name.lower()
    for node in graph_nodes:
        if node.get("resource_type") != "storage_bucket":
            continue
        node_name = (node.get("name") or "").lower()
        node_arn = (node.get("arn") or "").lower()
        if bucket_name_lower == node_name or bucket_name_lower in node_arn:
            return node
    return None


def _infer_from_env_vars(graph_nodes, enrichment_data):
    """Match environment variables that reference storage buckets.

    Returns list of edge dicts with confidence 0.7.
    """
    edges = []
    env_vars = enrichment_data.get("env_vars", {})

    # env_vars is expected as { service_name: { var_name: var_value, ... }, ... }
    for service_name, variables in env_vars.items():
        if not isinstance(variables, dict):
            continue
        for var_name, var_value in variables.items():
            bucket_name = _extract_bucket_from_env_value(var_value)
            if not bucket_name:
                continue

            bucket_node = _find_bucket_node(graph_nodes, bucket_name)
            if not bucket_node:
                continue

            edges.append({
                "from_service": service_name,
                "to_service": bucket_node["name"],
                "dependency_type": "storage",
                "confidence": 0.7,
                "discovered_from": ["env_var"],
                "detail": f"Env var {var_name} references bucket {bucket_name}",
            })

    return edges


def _infer_from_iam(graph_nodes, enrichment_data):
    """Match IAM roles with S3 access actions to bucket ARNs.

    Returns list of edge dicts with confidence 0.6.
    """
    edges = []
    iam_policies = enrichment_data.get("iam_policies", [])

    for policy in iam_policies:
        principal_name = policy.get("principal_name", "")
        statements = policy.get("statements", [])

        for statement in statements:
            if statement.get("Effect") != "Allow":
                continue

            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]

            has_s3_action = any(a in _S3_ACTIONS for a in actions)
            if not has_s3_action:
                continue

            resources = statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]

            for resource_arn in resources:
                match = _S3_ARN_PATTERN.match(resource_arn)
                if not match:
                    continue

                bucket_name = match.group(1)
                bucket_node = _find_bucket_node(graph_nodes, bucket_name)
                if not bucket_node:
                    continue

                # Find the compute node that has this IAM role attached
                compute_node = find_compute_node(graph_nodes, principal_name)
                if not compute_node:
                    continue

                edges.append({
                    "from_service": principal_name,
                    "to_service": bucket_node["name"],
                    "dependency_type": "storage",
                    "confidence": 0.6,
                    "discovered_from": ["iam"],
                    "detail": f"IAM policy grants {', '.join(actions)} on {bucket_name}",
                })

    return edges


def _infer_from_event_sources(graph_nodes, enrichment_data):
    """Match S3 event triggers (e.g. Lambda triggered by S3 notifications).

    Returns list of edge dicts with confidence 0.9.
    """
    edges = []
    event_sources = enrichment_data.get("lambda_event_sources", [])

    for mapping in event_sources:
        event_source_arn = mapping.get("EventSourceArn", "")
        function_arn = mapping.get("FunctionArn", "")

        if not event_source_arn or ":s3:::" not in event_source_arn:
            continue

        match = _S3_EVENT_SOURCE_PATTERN.match(event_source_arn)
        if not match:
            continue

        bucket_name = match.group(1)
        bucket_node = _find_bucket_node(graph_nodes, bucket_name)
        if not bucket_node:
            continue

        # Extract function name from ARN
        function_name = function_arn.split(":")[-1] if function_arn else ""
        compute_node = find_compute_node(graph_nodes, function_name)
        if not compute_node:
            continue

        edges.append({
            "from_service": bucket_node["name"],
            "to_service": function_name,
            "dependency_type": "storage",
            "confidence": 0.9,
            "discovered_from": ["event_source"],
            "detail": f"S3 bucket {bucket_name} triggers {function_name}",
        })

    return edges


def infer(user_id, graph_nodes, enrichment_data):
    """Run object storage inference.

    Determines which compute services access storage buckets by analyzing
    environment variables, IAM policies, and S3 event source mappings.

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of discovered graph node dicts.
        enrichment_data: Dict of enrichment data from Phase 2.

    Returns:
        List of dependency edge dicts.
    """
    edges = []

    edges.extend(_infer_from_env_vars(graph_nodes, enrichment_data))
    edges.extend(_infer_from_iam(graph_nodes, enrichment_data))
    edges.extend(_infer_from_event_sources(graph_nodes, enrichment_data))

    logger.info(
        "Storage inference for user %s: %d edges (env_var=%d, iam=%d, event=%d)",
        user_id,
        len(edges),
        sum(1 for e in edges if "env_var" in e["discovered_from"]),
        sum(1 for e in edges if "iam" in e["discovered_from"]),
        sum(1 for e in edges if "event_source" in e["discovered_from"]),
    )

    return edges
