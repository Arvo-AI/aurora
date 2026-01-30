"""
Load Balancer Target Mapping Inference - Phase 3 connection inference.

Processes ELBv2 target groups (from Phase 2 AWS enrichment) to create
explicit load balancer -> backend service dependency edges. Because target
groups are an explicit, declarative mapping in the load balancer configuration,
these edges receive maximum confidence (1.0).

Each target group contains:
    - ``load_balancer_arns``: The ARNs of load balancers using this group.
    - ``targets``: A list of TargetHealthDescription dicts, each containing
      a ``Target`` dict with ``Id`` (instance ID or IP) and ``Port``.

The module resolves load balancer ARNs and target IDs/IPs to service node
names using a combined lookup by ``cloud_resource_id`` and ``endpoint``.
"""

import logging

logger = logging.getLogger(__name__)


def _build_node_lookups(graph_nodes):
    """Build fast lookup dicts for resolving targets to service node names.

    Returns:
        Tuple of (by_resource_id, by_endpoint):
            by_resource_id: Dict mapping cloud_resource_id -> node name.
            by_endpoint: Dict mapping endpoint (IP or hostname) -> node name.
    """
    by_resource_id = {}
    by_endpoint = {}

    for node in graph_nodes:
        name = node.get("name")
        if not name:
            continue

        resource_id = node.get("cloud_resource_id", "")
        if resource_id:
            by_resource_id[resource_id] = name

        endpoint = node.get("endpoint", "")
        if endpoint:
            by_endpoint[endpoint] = name

        # Also index by private/public IPs for IP-based target groups
        for ip_field in ("private_ip", "public_ip"):
            ip_val = node.get(ip_field, "")
            if ip_val:
                by_endpoint[ip_val] = name

    return by_resource_id, by_endpoint


def _resolve_lb_name(lb_arn, by_resource_id):
    """Resolve a load balancer ARN to a node name.

    Tries exact match on the full ARN first, then falls back to matching
    by the resource portion of the ARN (the part after the last ``/``
    prefix for ``loadbalancer/`` or ``app/`` ARN formats).

    Args:
        lb_arn: Full ARN string of the load balancer.
        by_resource_id: Dict mapping cloud_resource_id -> node name.

    Returns:
        Node name string, or None if unresolved.
    """
    if not lb_arn:
        return None

    # Exact match
    if lb_arn in by_resource_id:
        return by_resource_id[lb_arn]

    # Try suffix matching (ARNs can be stored with or without region prefix)
    for key, name in by_resource_id.items():
        if key.endswith(lb_arn) or lb_arn.endswith(key):
            return name

    # Extract the load balancer name from the ARN
    # Format: arn:aws:elasticloadbalancing:region:acct:loadbalancer/app/name/id
    parts = lb_arn.split("/")
    if len(parts) >= 3:
        lb_name = parts[-2]  # The human-readable name
        for key, name in by_resource_id.items():
            if lb_name in key:
                return name

    return None


def _resolve_target(target_id, by_resource_id, by_endpoint):
    """Resolve a target group target to a node name.

    Targets can be instance IDs (e.g. ``i-0abc123``), IP addresses,
    or Lambda function ARNs.

    Args:
        target_id: The target ``Id`` from the target health description.
        by_resource_id: Dict mapping cloud_resource_id -> node name.
        by_endpoint: Dict mapping endpoint/IP -> node name.

    Returns:
        Node name string, or None if unresolved.
    """
    if not target_id:
        return None

    # Try resource ID lookup (instance IDs, Lambda ARNs)
    if target_id in by_resource_id:
        return by_resource_id[target_id]

    # Try endpoint/IP lookup
    if target_id in by_endpoint:
        return by_endpoint[target_id]

    # Try suffix matching for ARN-style targets
    for key, name in by_resource_id.items():
        if key.endswith(target_id) or target_id.endswith(key):
            return name

    return None


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from load balancer target group configurations.

    Processes ``enrichment_data["lb_target_groups"]`` -- a list of enriched
    target group dicts produced by Phase 2 AWS enrichment. Each target group
    maps one or more load balancers to their backend targets (EC2 instances,
    IPs, or Lambda functions).

    Args:
        user_id: The Aurora user ID performing inference.
        graph_nodes: List of service node dicts from Phase 1+2.
        enrichment_data: Dict of enrichment results. Reads:
            - ``lb_target_groups``: List of target group dicts, each with
              ``load_balancer_arns``, ``targets``, ``target_group_name``, etc.

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
    target_groups = enrichment_data.get("lb_target_groups", [])
    if not target_groups:
        logger.debug("No load balancer target group data found for user %s", user_id)
        return []

    by_resource_id, by_endpoint = _build_node_lookups(graph_nodes)
    if not by_resource_id and not by_endpoint:
        logger.warning("No graph nodes available for load balancer target resolution")
        return []

    edges = []
    seen = set()

    for tg in target_groups:
        tg_name = tg.get("target_group_name", "unknown")
        lb_arns = tg.get("load_balancer_arns", [])
        targets = tg.get("targets", [])

        # Resolve all load balancer ARNs to node names
        lb_names = []
        for lb_arn in lb_arns:
            lb_name = _resolve_lb_name(lb_arn, by_resource_id)
            if lb_name:
                lb_names.append(lb_name)
            else:
                logger.debug(
                    "Could not resolve LB ARN to node: %s (target group: %s)",
                    lb_arn, tg_name,
                )

        if not lb_names:
            logger.debug(
                "No load balancer nodes resolved for target group %s (%d ARNs)",
                tg_name, len(lb_arns),
            )
            continue

        # Resolve each target to a node name and create edges
        for target_desc in targets:
            target_info = target_desc.get("Target", {})
            target_id = target_info.get("Id", "")

            target_name = _resolve_target(target_id, by_resource_id, by_endpoint)
            if not target_name:
                logger.debug(
                    "Could not resolve target %s in target group %s",
                    target_id, tg_name,
                )
                continue

            for lb_name in lb_names:
                if lb_name == target_name:
                    continue

                edge_key = (lb_name, target_name)
                if edge_key in seen:
                    continue
                seen.add(edge_key)

                edges.append({
                    "from_service": lb_name,
                    "to_service": target_name,
                    "dependency_type": "load_balancer",
                    "confidence": 1.0,
                    "discovered_from": ["load_balancer"],
                })

    logger.info(
        "Load balancer inference complete for user %s: %d edges from %d target groups",
        user_id, len(edges), len(target_groups),
    )

    return edges
