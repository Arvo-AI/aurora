"""
Security Group / Firewall Inference - Phase 3 connection inference.

Analyzes security group rules (AWS) and network security group rules (Azure)
to infer DEPENDS_ON edges between service nodes. Security groups are the
strongest signal for non-Kubernetes dependencies because they explicitly
declare which resources are allowed to communicate on which ports.

For AWS, each security group contains inbound rules that reference either
a source security group (SG-to-SG) or a CIDR range. When a source SG is
attached to a known service node and the target SG is attached to another
known node, the inferred edge has high confidence (0.9). CIDR-based rules
are weaker (0.7) because they may overlap with multiple resources.

The port number from each rule is mapped to a dependency_type using the
shared ``infer_dependency_type_from_port`` helper from ``resource_mapper``.
"""

import logging

from services.discovery.resource_mapper import infer_dependency_type_from_port

logger = logging.getLogger(__name__)


def _build_sg_to_nodes_map(graph_nodes):
    """Map security group IDs to the list of node names attached to them.

    Each node may reference security groups in its ``security_groups`` field
    (a list of SG IDs) or in its ``cloud_resource_id`` (if the node itself
    is a security group resource).

    Returns:
        Dict mapping security group ID (str) -> list of node name strings.
    """
    sg_to_nodes = {}
    for node in graph_nodes:
        name = node.get("name")
        if not name:
            continue

        # A node may list the SGs it belongs to
        sg_ids = node.get("security_groups") or []
        if isinstance(sg_ids, str):
            sg_ids = [sg_ids]
        for sg_id in sg_ids:
            sg_to_nodes.setdefault(sg_id, []).append(name)

        # If the node's cloud_resource_id looks like a security group, index it
        resource_id = node.get("cloud_resource_id", "")
        if resource_id.startswith("sg-"):
            sg_to_nodes.setdefault(resource_id, []).append(name)

    return sg_to_nodes


def _build_vpc_cidr_to_nodes_map(graph_nodes):
    """Map VPC IDs and private IPs/CIDRs to node names for CIDR-based matching.

    Returns:
        Tuple of (ip_to_nodes, vpc_nodes):
            ip_to_nodes: Dict mapping IP address (str) -> list of node names.
            vpc_nodes: Dict mapping vpc_id (str) -> list of node names in that VPC.
    """
    ip_to_nodes = {}
    vpc_nodes = {}

    for node in graph_nodes:
        name = node.get("name")
        if not name:
            continue

        # Index by private/public IP if available
        for ip_field in ("private_ip", "public_ip", "endpoint"):
            ip_val = node.get(ip_field, "")
            if ip_val and isinstance(ip_val, str) and not ip_val.startswith("http"):
                ip_to_nodes.setdefault(ip_val, []).append(name)

        # Index by VPC
        vpc_id = node.get("vpc_id", "")
        if vpc_id:
            vpc_nodes.setdefault(vpc_id, []).append(name)

    return ip_to_nodes, vpc_nodes


def _infer_aws_sg_edges(security_groups, graph_nodes):
    """Infer dependency edges from AWS security group inbound rules.

    Args:
        security_groups: List of AWS SecurityGroup dicts (from describe-security-groups).
        graph_nodes: List of service node dicts.

    Returns:
        List of dependency edge dicts.
    """
    sg_to_nodes = _build_sg_to_nodes_map(graph_nodes)
    edges = []
    seen = set()

    for sg in security_groups:
        sg_id = sg.get("GroupId", "")
        target_nodes = sg_to_nodes.get(sg_id, [])
        if not target_nodes:
            continue

        inbound_rules = sg.get("IpPermissions", [])
        for rule in inbound_rules:
            from_port = rule.get("FromPort")
            to_port = rule.get("ToPort")

            # Determine dependency type from port
            port = from_port if from_port == to_port else from_port
            if port is not None:
                dep_type, _ = infer_dependency_type_from_port(port)
            else:
                dep_type = "network"

            # --- SG-to-SG references (high confidence) ---
            source_sg_refs = rule.get("UserIdGroupPairs", [])
            for sg_ref in source_sg_refs:
                source_sg_id = sg_ref.get("GroupId", "")
                source_nodes = sg_to_nodes.get(source_sg_id, [])

                for source_name in source_nodes:
                    for target_name in target_nodes:
                        if source_name == target_name:
                            continue
                        edge_key = (source_name, target_name, dep_type)
                        if edge_key in seen:
                            continue
                        seen.add(edge_key)

                        edges.append({
                            "from_service": source_name,
                            "to_service": target_name,
                            "dependency_type": dep_type,
                            "confidence": 0.9,
                            "discovered_from": ["security_group"],
                        })

            # --- CIDR-based rules (lower confidence) ---
            cidr_ranges = rule.get("IpRanges", [])
            for cidr_entry in cidr_ranges:
                cidr = cidr_entry.get("CidrIp", "")
                if not cidr:
                    continue

                # Skip broad rules (0.0.0.0/0) -- they don't point to a specific source
                if cidr in ("0.0.0.0/0", "::/0"):
                    continue

                # Try to match CIDR to a known node by IP
                # For /32 CIDRs, extract the IP
                source_ip = cidr.split("/")[0] if "/" in cidr else cidr
                # Look for nodes whose private/public IP matches
                for node in graph_nodes:
                    node_name = node.get("name", "")
                    if not node_name:
                        continue
                    node_ips = set()
                    for ip_field in ("private_ip", "public_ip", "endpoint"):
                        ip_val = node.get(ip_field, "")
                        if ip_val and isinstance(ip_val, str) and not ip_val.startswith("http"):
                            node_ips.add(ip_val)

                    if source_ip in node_ips:
                        for target_name in target_nodes:
                            if node_name == target_name:
                                continue
                            edge_key = (node_name, target_name, dep_type)
                            if edge_key in seen:
                                continue
                            seen.add(edge_key)

                            edges.append({
                                "from_service": node_name,
                                "to_service": target_name,
                                "dependency_type": dep_type,
                                "confidence": 0.7,
                                "discovered_from": ["security_group"],
                            })

    return edges


def _infer_azure_nsg_edges(nsg_rules, graph_nodes):
    """Infer dependency edges from Azure Network Security Group rules.

    Args:
        nsg_rules: List of Azure NSG rule dicts.
        graph_nodes: List of service node dicts.

    Returns:
        List of dependency edge dicts.
    """
    edges = []
    seen = set()

    # Build lookup by name and resource ID
    nodes_by_id = {}
    nodes_by_name = {}
    for node in graph_nodes:
        name = node.get("name", "")
        resource_id = node.get("cloud_resource_id", "")
        if name:
            nodes_by_name[name.lower()] = name
        if resource_id:
            nodes_by_id[resource_id] = name

    for rule in nsg_rules:
        direction = rule.get("direction", "").lower()
        if direction != "inbound":
            continue

        access = rule.get("access", "").lower()
        if access != "allow":
            continue

        dest_port = rule.get("destination_port_range", "")
        source_address = rule.get("source_address_prefix", "")
        dest_address = rule.get("destination_address_prefix", "")

        # Determine dependency type from port
        dep_type = "network"
        if dest_port and dest_port != "*":
            try:
                port_num = int(dest_port)
                dep_type, _ = infer_dependency_type_from_port(port_num)
            except ValueError:
                pass

        # Resolve source and destination to node names
        source_name = nodes_by_id.get(source_address) or nodes_by_name.get(
            source_address.lower() if source_address else ""
        )
        dest_name = nodes_by_id.get(dest_address) or nodes_by_name.get(
            dest_address.lower() if dest_address else ""
        )

        if not source_name or not dest_name or source_name == dest_name:
            continue

        edge_key = (source_name, dest_name, dep_type)
        if edge_key in seen:
            continue
        seen.add(edge_key)

        edges.append({
            "from_service": source_name,
            "to_service": dest_name,
            "dependency_type": dep_type,
            "confidence": 0.7,
            "discovered_from": ["security_group"],
        })

    return edges


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from security group and firewall rules.

    Processes AWS security groups (``enrichment_data["security_groups"]``)
    and Azure NSG rules (``enrichment_data["nsg_rules"]``) to determine
    which services are allowed to communicate with each other, and on
    which ports.

    Args:
        user_id: The Aurora user ID performing inference.
        graph_nodes: List of service node dicts from Phase 1+2.
        enrichment_data: Dict of enrichment results. Reads:
            - ``security_groups``: List of AWS SecurityGroup dicts.
            - ``nsg_rules``: List of Azure NSG rule dicts.

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
    edges = []

    # --- AWS Security Groups ---
    aws_sgs = enrichment_data.get("security_groups", [])
    if aws_sgs:
        logger.info(
            "Processing %d AWS security groups for user %s",
            len(aws_sgs), user_id,
        )
        aws_edges = _infer_aws_sg_edges(aws_sgs, graph_nodes)
        edges.extend(aws_edges)
        logger.info("AWS security group inference produced %d edges", len(aws_edges))

    # --- Azure NSG Rules ---
    # nsg_rules is a dict {nsg_name: [rules]} — flatten to a single list
    azure_nsg_data = enrichment_data.get("nsg_rules", {})
    azure_nsgs = []
    if isinstance(azure_nsg_data, dict):
        for rules in azure_nsg_data.values():
            if isinstance(rules, list):
                azure_nsgs.extend(rules)
    elif isinstance(azure_nsg_data, list):
        azure_nsgs = azure_nsg_data
    if azure_nsgs:
        logger.info(
            "Processing %d Azure NSG rules for user %s",
            len(azure_nsgs), user_id,
        )
        azure_edges = _infer_azure_nsg_edges(azure_nsgs, graph_nodes)
        edges.extend(azure_edges)
        logger.info("Azure NSG inference produced %d edges", len(azure_edges))

    if not aws_sgs and not azure_nsgs:
        logger.debug("No security group data found in enrichment_data for user %s", user_id)

    logger.info(
        "Security group inference complete for user %s: %d total edges",
        user_id, len(edges),
    )

    return edges
