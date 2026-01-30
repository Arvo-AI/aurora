"""
DNS Record Resolution - Phase 3 inference engine.

Infers DEPENDS_ON edges by resolving DNS records (A, AAAA, CNAME, ALIAS)
to service nodes. When a DNS zone has records pointing at a service's IP
or hostname, a dependency edge is created from the DNS zone to the target.
"""

import logging
from ipaddress import ip_address

logger = logging.getLogger(__name__)


def _normalize_hostname(hostname):
    """Strip trailing dot and lowercase a hostname for comparison."""
    if not hostname:
        return ""
    return hostname.rstrip(".").lower()


def _build_ip_index(graph_nodes):
    """Build a mapping from IP address -> node name for fast lookups."""
    ip_index = {}
    for node in graph_nodes:
        endpoint = (node.get("endpoint") or "").strip()
        if not endpoint:
            continue
        try:
            ip_address(endpoint)
            ip_index[endpoint] = node["name"]
        except ValueError:
            pass
    return ip_index


def _build_hostname_index(graph_nodes):
    """Build a mapping from normalized hostname -> node name.

    Indexes both the endpoint field (if it looks like a hostname) and
    any hostname that can be extracted from the cloud_resource_id
    (e.g. ELB ARNs contain the load balancer name which appears in
    its DNS hostname).
    """
    hostname_index = {}
    for node in graph_nodes:
        endpoint = _normalize_hostname(node.get("endpoint"))
        if endpoint:
            try:
                ip_address(endpoint)
            except ValueError:
                # Not an IP, treat as hostname
                hostname_index[endpoint] = node["name"]

        cloud_id = (node.get("cloud_resource_id") or "").lower()
        if cloud_id:
            hostname_index[cloud_id] = node["name"]
    return hostname_index


def _match_cname_to_node(cname_value, hostname_index):
    """Try to match a CNAME/ALIAS target to a graph node.

    First attempts an exact match on the hostname index, then falls
    back to substring matching against cloud_resource_ids (handles
    cases like ELB DNS names containing the ARN load balancer name).
    """
    normalized = _normalize_hostname(cname_value)
    if not normalized:
        return None

    # Exact match
    if normalized in hostname_index:
        return hostname_index[normalized]

    # Substring match: check if any indexed hostname is contained in
    # the CNAME target or vice-versa (e.g. ELB hostname fragments)
    for indexed_hostname, node_name in hostname_index.items():
        if indexed_hostname in normalized or normalized in indexed_hostname:
            return node_name

    return None


def _find_dns_zone_node(zone_name, graph_nodes):
    """Find the graph node corresponding to a DNS hosted zone by name."""
    normalized_zone = _normalize_hostname(zone_name)
    for node in graph_nodes:
        if node.get("resource_type") == "dns_zone":
            if _normalize_hostname(node.get("name")) == normalized_zone:
                return node["name"]
    return None


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from DNS records.

    Processes enrichment_data["dns_records"] which contains Route 53
    hosted zones (or equivalent). For each record set in each zone:
      - A/AAAA records: match the IP value to a node endpoint
      - CNAME/ALIAS records: match the hostname value to a node endpoint
        or cloud_resource_id

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of service node dicts from Phase 1.
        enrichment_data: Dict from Phase 2 enrichment.

    Returns:
        List of dependency edge dicts with keys: from_service, to_service,
        dependency_type, confidence, discovered_from.
    """
    dns_records = enrichment_data.get("dns_records", [])
    if not dns_records:
        logger.debug("No DNS records in enrichment data for user %s", user_id)
        return []

    ip_index = _build_ip_index(graph_nodes)
    hostname_index = _build_hostname_index(graph_nodes)
    edges = []
    seen = set()

    for zone in dns_records:
        zone_name = zone.get("Name") or zone.get("name", "")
        zone_node_name = _find_dns_zone_node(zone_name, graph_nodes)

        # If we cannot find a matching DNS zone node, use the zone name
        # itself so the edge is still recorded (graph_writer will skip
        # edges with unknown source nodes, but the data is available for
        # debugging).
        source_name = zone_node_name or _normalize_hostname(zone_name)
        if not source_name:
            continue

        record_sets = zone.get("ResourceRecordSets", zone.get("record_sets", []))
        for record in record_sets:
            record_type = (record.get("Type") or record.get("type", "")).upper()
            resource_records = record.get("ResourceRecords", record.get("records", []))

            for rr in resource_records:
                value = (rr.get("Value") or rr.get("value", "")).strip()
                if not value:
                    continue

                target_node = None
                if record_type in ("A", "AAAA"):
                    target_node = ip_index.get(value)
                elif record_type in ("CNAME", "ALIAS"):
                    target_node = _match_cname_to_node(value, hostname_index)

                if target_node and target_node != source_name:
                    edge_key = (source_name, target_node)
                    if edge_key not in seen:
                        seen.add(edge_key)
                        edges.append({
                            "from_service": source_name,
                            "to_service": target_node,
                            "dependency_type": "dns",
                            "confidence": 0.8,
                            "discovered_from": ["dns"],
                        })

            # Also handle Route 53 alias targets (AliasTarget field)
            alias_target = record.get("AliasTarget", record.get("alias_target"))
            if alias_target:
                alias_dns = alias_target.get("DNSName") or alias_target.get("dns_name", "")
                target_node = _match_cname_to_node(alias_dns, hostname_index)
                if target_node and target_node != source_name:
                    edge_key = (source_name, target_node)
                    if edge_key not in seen:
                        seen.add(edge_key)
                        edges.append({
                            "from_service": source_name,
                            "to_service": target_node,
                            "dependency_type": "dns",
                            "confidence": 0.8,
                            "discovered_from": ["dns"],
                        })

    logger.info(
        "DNS inference for user %s: %d edges from %d zones",
        user_id, len(edges), len(dns_records),
    )
    return edges
