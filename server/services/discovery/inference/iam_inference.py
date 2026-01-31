"""
IAM Role Binding Analysis - Phase 3 inference engine.

Infers DEPENDS_ON edges from IAM policies attached to compute resources.
When a Lambda function or EC2 instance has an IAM role whose policy grants
access to specific resources (S3 buckets, DynamoDB tables, SQS queues, etc.),
a weak dependency edge is created.

This is the weakest signal among the dedicated inference engines (confidence 0.6)
because IAM permissions are often over-provisioned.
"""

import json
import logging

from services.discovery.inference.node_lookup import find_node_by_arn, find_node_by_name

logger = logging.getLogger(__name__)

# Maps AWS IAM action prefixes to dependency types.
_ACTION_PREFIX_TO_DEPENDENCY = {
    "s3": "storage",
    "dynamodb": "database",
    "rds": "database",
    "rds-db": "database",
    "rds-data": "database",
    "sqs": "queue",
    "sns": "messaging",
    "kinesis": "streaming",
    "secretsmanager": "secret_access",
    "ssm": "secret_access",
    "elasticache": "cache",
    "es": "search",
    "opensearch": "search",
    "states": "orchestration",
    "lambda": "invocation",
    "execute-api": "api",
}


def _parse_dependency_type_from_actions(actions):
    """Determine the dependency type from a list of IAM actions.

    Args:
        actions: List of IAM action strings (e.g. ["s3:GetObject", "s3:PutObject"]).

    Returns:
        Dependency type string, or "iam" as fallback.
    """
    if not actions:
        return "iam"

    for action in actions:
        if not isinstance(action, str):
            continue
        # Wildcard action gives no useful signal
        if action == "*":
            continue
        prefix = action.split(":")[0].lower()
        dep_type = _ACTION_PREFIX_TO_DEPENDENCY.get(prefix)
        if dep_type:
            return dep_type

    return "iam"


def _extract_arns_from_resource(resource_field):
    """Extract ARN strings from a policy statement's Resource field.

    Resource can be a single string or a list of strings.
    """
    if isinstance(resource_field, str):
        return [resource_field] if resource_field != "*" else []
    if isinstance(resource_field, list):
        return [r for r in resource_field if isinstance(r, str) and r != "*"]
    return []


def _find_compute_nodes(graph_nodes):
    """Return graph nodes that represent compute resources (Lambda, EC2, ECS, etc.)."""
    compute_types = {"vm", "serverless_function", "kubernetes_cluster"}
    return [
        node for node in graph_nodes
        if node.get("resource_type") in compute_types
    ]


def _infer_from_aws_policies(iam_policies, graph_nodes):
    """Infer edges from AWS IAM policy documents.

    iam_policies is expected to be a dict mapping resource name/ARN to
    a list of policy documents (each being a dict with "Statement" list).
    """
    edges = []
    seen = set()

    for resource_id, policies in iam_policies.items():
        # Find the compute node that holds this role
        source_node = find_node_by_arn(resource_id, graph_nodes)
        if not source_node:
            source_node = find_node_by_name(resource_id, graph_nodes)
        if not source_node:
            continue

        for policy in policies:
            statements = []
            if isinstance(policy, dict):
                statements = policy.get("Statement", [])
            elif isinstance(policy, str):
                try:
                    parsed = json.loads(policy)
                    statements = parsed.get("Statement", [])
                except (json.JSONDecodeError, AttributeError):
                    continue

            for statement in statements:
                if not isinstance(statement, dict):
                    continue
                effect = (statement.get("Effect") or "").lower()
                if effect != "allow":
                    continue

                actions = statement.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]

                resource_arns = _extract_arns_from_resource(
                    statement.get("Resource", [])
                )

                dep_type = _parse_dependency_type_from_actions(actions)

                for arn in resource_arns:
                    target_node = find_node_by_arn(arn, graph_nodes)
                    if not target_node or target_node == source_node:
                        continue

                    edge_key = (source_node, target_node)
                    if edge_key not in seen:
                        seen.add(edge_key)
                        edges.append({
                            "from_service": source_node,
                            "to_service": target_node,
                            "dependency_type": dep_type,
                            "confidence": 0.6,
                            "discovered_from": ["iam"],
                        })

    return edges


def _infer_from_gcp_bindings(iam_policies, graph_nodes):
    """Infer edges from GCP IAM policy bindings.

    GCP iam_policies is expected to be a dict mapping resource name to
    a list of binding dicts, each with "role" and "members" fields.
    Members that reference service accounts of compute resources suggest
    the compute resource depends on the target resource.
    """
    edges = []
    seen = set()

    # Build a lookup from service account email to compute node name
    sa_to_node = {}
    compute_nodes = _find_compute_nodes(graph_nodes)
    for node in compute_nodes:
        # Service accounts are often named after the resource
        node_name_lower = (node.get("name") or "").lower()
        sa_to_node[node_name_lower] = node["name"]

    for resource_name, bindings in iam_policies.items():
        target_node = find_node_by_name(resource_name, graph_nodes)
        if not target_node:
            continue

        if not isinstance(bindings, list):
            continue

        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            members = binding.get("members", [])
            for member in members:
                if not isinstance(member, str):
                    continue
                # Extract service account name: serviceAccount:name@project.iam...
                if member.startswith("serviceAccount:"):
                    sa_email = member.split(":", 1)[1]
                    sa_name = sa_email.split("@")[0].lower()
                    source_node = sa_to_node.get(sa_name)
                    if source_node and source_node != target_node:
                        edge_key = (source_node, target_node)
                        if edge_key not in seen:
                            seen.add(edge_key)
                            edges.append({
                                "from_service": source_node,
                                "to_service": target_node,
                                "dependency_type": "iam",
                                "confidence": 0.6,
                                "discovered_from": ["iam"],
                            })

    return edges


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from IAM role bindings and policy documents.

    Examines IAM policies attached to compute resources and matches
    Resource ARNs in Allow statements to service graph nodes. This is
    a weak signal (confidence 0.6) since IAM policies are often broader
    than actual usage.

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of service node dicts from Phase 1.
        enrichment_data: Dict from Phase 2 enrichment.

    Returns:
        List of dependency edge dicts with keys: from_service, to_service,
        dependency_type, confidence, discovered_from.
    """
    iam_policies = enrichment_data.get("iam_policies", {})
    if not iam_policies:
        logger.debug("No IAM policies in enrichment data for user %s", user_id)
        return []

    edges = []

    # Detect whether this is AWS-style (policy documents with Statement)
    # or GCP-style (bindings with role/members) by inspecting the first value.
    first_value = next(iter(iam_policies.values()), None)
    is_gcp_style = False
    if isinstance(first_value, list) and first_value:
        sample = first_value[0]
        if isinstance(sample, dict) and "role" in sample:
            is_gcp_style = True

    if is_gcp_style:
        edges = _infer_from_gcp_bindings(iam_policies, graph_nodes)
    else:
        edges = _infer_from_aws_policies(iam_policies, graph_nodes)

    logger.info(
        "IAM inference for user %s: %d edges from %d policy entries",
        user_id, len(edges), len(iam_policies),
    )
    return edges
