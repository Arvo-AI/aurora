"""
Kubectl Discovery Provider - Discovers K8s resources from on-prem clusters
connected via the kubectl agent WebSocket bridge.

Phase 1 provider that executes kubectl commands through the chatbot's internal
API endpoint, then reuses extraction/relationship functions from
kubernetes_enrichment.py.
"""

import json
import logging
import os

import requests

from services.discovery.enrichment.kubernetes_enrichment import (
    _extract_workload_node,
    _extract_service_node,
    _extract_ingress_node,
    _build_relationships,
)

logger = logging.getLogger(__name__)

PROVIDER = "kubectl"
REQUEST_TIMEOUT = 120


def _execute_kubectl(user_id, cluster_id, command):
    """Execute a kubectl command on a remote cluster via the chatbot internal API.

    Args:
        user_id: The user performing discovery.
        cluster_id: The cluster to execute against.
        command: The kubectl command string (e.g. "kubectl get deployments -A -o json").

    Returns:
        Tuple of (parsed_json_or_None, error_string_or_None).
    """
    chatbot_url = os.getenv("CHATBOT_INTERNAL_URL")
    if not chatbot_url:
        return None, "CHATBOT_INTERNAL_URL not configured"

    secret = os.getenv("FLASK_SECRET_KEY")
    if not secret:
        return None, "FLASK_SECRET_KEY not configured"

    try:
        response = requests.post(
            f"{chatbot_url}/internal/kubectl/execute",
            json={
                "user_id": user_id,
                "cluster_id": cluster_id,
                "command": command,
                "timeout": REQUEST_TIMEOUT,
            },
            headers={"X-Internal-Secret": secret},
            timeout=REQUEST_TIMEOUT + 10,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            return None, data.get("error", "Command failed")

        output = data.get("output", "")
        if not output:
            return {"items": []}, None

        return json.loads(output), None
    except requests.exceptions.Timeout:
        return None, f"Request timed out after {REQUEST_TIMEOUT}s"
    except requests.exceptions.RequestException as e:
        return None, f"Request failed: {e}"
    except json.JSONDecodeError as e:
        return None, f"Failed to parse JSON output: {e}"


def _discover_cluster(user_id, cluster_id, cluster_name):
    """Discover internal resources for a single kubectl-connected cluster.

    Args:
        user_id: The user performing discovery.
        cluster_id: The cluster ID for the kubectl agent.
        cluster_name: Human-readable cluster name.

    Returns:
        Tuple of (nodes_list, relationships_list, errors_list).
    """
    nodes = []
    errors = []

    # Build a cluster dict matching the shape kubernetes_enrichment expects
    cluster = {
        "name": cluster_name,
        "provider": PROVIDER,
        "region": "on-prem",
    }

    # Add the cluster itself as a node
    cluster_node = {
        "id": f"kubectl:{cluster_id}",
        "name": cluster_name,
        "resource_type": "kubernetes_cluster",
        "sub_type": "on_prem",
        "provider": PROVIDER,
        "region": "on-prem",
        "metadata": {
            "cluster_id": cluster_id,
        },
    }
    nodes.append(cluster_node)

    # Fetch all resource types
    # The agent prepends 'kubectl' automatically, so only send the arguments
    kubectl_commands = {
        "deployments": "get deployments -A -o json",
        "statefulsets": "get statefulsets -A -o json",
        "daemonsets": "get daemonsets -A -o json",
        "services": "get services -A -o json",
        "ingresses": "get ingresses -A -o json",
    }

    raw_resources = {}
    for resource_kind, cmd in kubectl_commands.items():
        logger.info(f"kubectl discovery: fetching {resource_kind} from cluster {cluster_name}")
        data, error = _execute_kubectl(user_id, cluster_id, cmd)
        if error:
            error_msg = (
                f"Failed to fetch {resource_kind} from cluster "
                f"{cluster_name}: {error}"
            )
            logger.warning(error_msg)
            errors.append(error_msg)
            raw_resources[resource_kind] = []
        else:
            raw_resources[resource_kind] = data.get("items", [])

    # Extract nodes using kubernetes_enrichment helpers
    workload_nodes = []
    service_nodes = []
    ingress_backends = []

    for item in raw_resources.get("deployments", []):
        workload_nodes.append(_extract_workload_node(item, "Deployment", cluster))

    for item in raw_resources.get("statefulsets", []):
        workload_nodes.append(_extract_workload_node(item, "StatefulSet", cluster))

    for item in raw_resources.get("daemonsets", []):
        workload_nodes.append(_extract_workload_node(item, "DaemonSet", cluster))

    for item in raw_resources.get("services", []):
        service_nodes.append(_extract_service_node(item, cluster))

    for item in raw_resources.get("ingresses", []):
        node, backend_svc_names = _extract_ingress_node(item, cluster)
        nodes.append(node)
        namespace = item.get("metadata", {}).get("namespace", "default")
        ingress_backends.append((node["name"], namespace, backend_svc_names))

    nodes.extend(workload_nodes)
    nodes.extend(service_nodes)

    # Build edges
    relationships = _build_relationships(
        ingress_backends, service_nodes, workload_nodes, cluster_name
    )

    logger.info(
        f"kubectl discovery for cluster {cluster_name}: "
        f"{len(nodes)} nodes, {len(relationships)} edges"
    )

    return nodes, relationships, errors


def discover(user_id, credentials, env=None):
    """Discover K8s resources from on-prem clusters connected via kubectl agent.

    Args:
        user_id: The user performing discovery.
        credentials: Dict with key:
            - clusters: List of dicts with cluster_id and cluster_name.
        env: Unused (kubectl uses the chatbot internal API). Accepted for
             interface consistency with other providers.

    Returns:
        Dict with keys: nodes (list), relationships (list), errors (list).
    """
    all_nodes = []
    all_relationships = []
    all_errors = []

    clusters = credentials.get("clusters", [])
    if not clusters:
        logger.info("kubectl discovery: no connected clusters")
        return {"nodes": [], "relationships": [], "errors": []}

    logger.info(
        f"kubectl discovery: discovering {len(clusters)} clusters for user {user_id}"
    )

    for cluster_info in clusters:
        cluster_id = cluster_info.get("cluster_id")
        cluster_name = cluster_info.get("cluster_name", cluster_id)

        if not cluster_id:
            all_errors.append("kubectl discovery: cluster entry missing cluster_id")
            continue

        try:
            nodes, relationships, errors = _discover_cluster(
                user_id, cluster_id, cluster_name
            )
            all_nodes.extend(nodes)
            all_relationships.extend(relationships)
            all_errors.extend(errors)
        except Exception as e:
            error_msg = f"kubectl discovery failed for cluster {cluster_name}: {e}"
            logger.exception(error_msg)
            all_errors.append(error_msg)

    logger.info(
        f"kubectl discovery complete for user {user_id}: "
        f"{len(all_nodes)} nodes, {len(all_relationships)} relationships, "
        f"{len(all_errors)} errors"
    )

    return {
        "nodes": all_nodes,
        "relationships": all_relationships,
        "errors": all_errors,
    }
