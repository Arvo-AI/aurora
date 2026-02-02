"""
Kubernetes Internal Discovery Enrichment (Phase 2).

Runs after Phase 1 discovers K8s clusters (GKE, EKS, AKS). For each cluster,
authenticates via the appropriate CLI, then uses kubectl to discover internal
workloads (Deployments, StatefulSets, DaemonSets, Services, Ingresses) and
maps them into normalized graph nodes and dependency edges.
"""

import json
import logging
import subprocess

from services.discovery.resource_mapper import infer_type_from_image

logger = logging.getLogger(__name__)

# Timeout for credential and kubectl commands (seconds)
CREDENTIALS_TIMEOUT = 60
KUBECTL_TIMEOUT = 120

# Confidence score for Kubernetes-derived edges
K8S_EDGE_CONFIDENCE = 0.9

# kubectl resource commands to run against each cluster
KUBECTL_COMMANDS = {
    "deployments": ["kubectl", "get", "deployments", "-A", "-o", "json"],
    "statefulsets": ["kubectl", "get", "statefulsets", "-A", "-o", "json"],
    "daemonsets": ["kubectl", "get", "daemonsets", "-A", "-o", "json"],
    "services": ["kubectl", "get", "services", "-A", "-o", "json"],
    "ingresses": ["kubectl", "get", "ingresses", "-A", "-o", "json"],
}


# =========================================================================
# CLI Helpers
# =========================================================================


def _run_command(args, timeout=KUBECTL_TIMEOUT):
    """Run a CLI command and return (stdout_string, error_string_or_None).

    Args:
        args: List of command arguments.
        timeout: Command timeout in seconds.

    Returns:
        Tuple of (stdout_str, error_str_or_None).
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return None, f"Command failed (rc={result.returncode}): {stderr}"
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, f"Command timed out after {timeout}s: {' '.join(args)}"
    except Exception as e:
        return None, f"Command error: {e}"


def _run_json_command(args, timeout=KUBECTL_TIMEOUT):
    """Run a CLI command expecting JSON output.

    Returns:
        Tuple of (parsed_json, error_string_or_None).
    """
    stdout, error = _run_command(args, timeout=timeout)
    if error:
        return None, error
    if not stdout:
        return {"items": []}, None
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as e:
        return None, f"Failed to parse JSON output: {e}"


# =========================================================================
# Cluster Credential Retrieval
# =========================================================================


def _get_cluster_credentials(cluster, provider_credentials):
    """Authenticate kubectl to a cluster using the appropriate cloud CLI.

    Args:
        cluster: Cluster dict from Phase 1 with name, provider, region, zone,
                 cloud_resource_id.
        provider_credentials: Dict of provider credentials (may include
                              project, resource_group, etc.).

    Returns:
        Error string or None on success.
    """
    provider = cluster.get("provider", "").lower()
    cluster_name = cluster.get("name", "")
    region = cluster.get("region", "")
    zone = cluster.get("zone", "")

    if provider == "gcp":
        project = provider_credentials.get("project") or cluster.get("project", "")
        location = zone or region
        if not location:
            return f"GKE cluster {cluster_name}: missing zone/region"
        args = [
            "gcloud", "container", "clusters", "get-credentials",
            cluster_name,
            "--zone", location,
            "--project", project,
        ]
        _, error = _run_command(args, timeout=CREDENTIALS_TIMEOUT)
        return error

    elif provider == "aws":
        if not region:
            return f"EKS cluster {cluster_name}: missing region"
        args = [
            "aws", "eks", "update-kubeconfig",
            "--name", cluster_name,
            "--region", region,
        ]
        _, error = _run_command(args, timeout=CREDENTIALS_TIMEOUT)
        return error

    elif provider == "azure":
        resource_group = provider_credentials.get("resource_group") or \
            cluster.get("resource_group", "")
        if not resource_group:
            return f"AKS cluster {cluster_name}: missing resource_group"
        args = [
            "az", "aks", "get-credentials",
            "--name", cluster_name,
            "--resource-group", resource_group,
            "--overwrite-existing",
        ]
        _, error = _run_command(args, timeout=CREDENTIALS_TIMEOUT)
        return error

    else:
        return f"Unsupported Kubernetes provider: {provider}"


# =========================================================================
# Resource Extraction Helpers
# =========================================================================


def _get_primary_image(containers):
    """Return the image name of the first container, or None."""
    if not containers:
        return None
    return containers[0].get("image", None)


def _extract_workload_node(item, kind, cluster):
    """Extract a graph node dict from a K8s workload item.

    Args:
        item: A single item from kubectl JSON output.
        kind: One of 'Deployment', 'StatefulSet', 'DaemonSet'.
        cluster: Parent cluster dict.

    Returns:
        Node dict.
    """
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    name = metadata.get("name", "")
    namespace = metadata.get("namespace", "default")
    cluster_name = cluster.get("name", "")
    provider = cluster.get("provider", "")
    region = cluster.get("region", "")

    # Get the primary container image for type inference
    pod_spec = spec.get("template", {}).get("spec", {})
    containers = pod_spec.get("containers", [])
    image = _get_primary_image(containers)

    inferred_type, inferred_sub_type = infer_type_from_image(image)

    # Determine resource_type based on kind and image heuristics
    if kind == "Deployment":
        resource_type = inferred_type or "kubernetes_deployment"
        sub_type = inferred_sub_type or "deployment"
    elif kind == "StatefulSet":
        # StatefulSets are typically databases or caches
        resource_type = inferred_type or "database"
        sub_type = inferred_sub_type or "statefulset"
    elif kind == "DaemonSet":
        resource_type = "kubernetes_deployment"
        sub_type = inferred_sub_type or "daemonset"
    else:
        resource_type = inferred_type or "kubernetes_deployment"
        sub_type = inferred_sub_type or kind.lower()

    node = {
        "name": name,
        "display_name": f"{name} ({namespace})",
        "resource_type": resource_type,
        "sub_type": sub_type,
        "provider": provider,
        "region": region,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "metadata": {
            "kind": kind,
            "image": image,
            "replicas": spec.get("replicas"),
            "labels": metadata.get("labels", {}),
        },
    }

    # Attach selector labels for service-to-deployment matching
    match_labels = spec.get("selector", {}).get("matchLabels", {})
    if match_labels:
        node["metadata"]["match_labels"] = match_labels

    return node


def _extract_service_node(item, cluster):
    """Extract a graph node dict from a K8s Service item.

    Args:
        item: A single Service item from kubectl JSON output.
        cluster: Parent cluster dict.

    Returns:
        Node dict.
    """
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    name = metadata.get("name", "")
    namespace = metadata.get("namespace", "default")
    cluster_name = cluster.get("name", "")
    provider = cluster.get("provider", "")
    region = cluster.get("region", "")

    svc_type = spec.get("type", "ClusterIP")

    if svc_type == "LoadBalancer":
        resource_type = "load_balancer"
        sub_type = "kubernetes_lb"
    else:
        resource_type = "kubernetes_service"
        sub_type = svc_type.lower()

    # Build endpoint from the first port
    ports = spec.get("ports", [])
    port = ports[0].get("port") if ports else None
    endpoint = f"{name}.{namespace}.svc.cluster.local"
    if port:
        endpoint = f"{endpoint}:{port}"

    # Prefix with svc/ to avoid ID collision with workloads of the same name
    svc_name = f"svc/{name}"

    node = {
        "name": svc_name,
        "display_name": f"svc/{name} ({namespace})",
        "resource_type": resource_type,
        "sub_type": sub_type,
        "provider": provider,
        "region": region,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "endpoint": endpoint,
        "metadata": {
            "kind": "Service",
            "service_type": svc_type,
            "ports": ports,
            "selector": spec.get("selector", {}),
            "labels": metadata.get("labels", {}),
            "k8s_name": name,
        },
    }

    return node


def _extract_ingress_node(item, cluster):
    """Extract a graph node dict from a K8s Ingress item.

    Args:
        item: A single Ingress item from kubectl JSON output.
        cluster: Parent cluster dict.

    Returns:
        Tuple of (node_dict, list_of_backend_service_names).
    """
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    name = metadata.get("name", "")
    namespace = metadata.get("namespace", "default")
    cluster_name = cluster.get("name", "")
    provider = cluster.get("provider", "")
    region = cluster.get("region", "")

    # Collect hosts from rules
    rules = spec.get("rules", [])
    hosts = [r.get("host") for r in rules if r.get("host")]

    # Collect backend service names for edge generation
    backend_services = set()
    for rule in rules:
        http = rule.get("http", {})
        for path in http.get("paths", []):
            backend = path.get("backend", {})
            svc = backend.get("service", {})
            svc_name = svc.get("name")
            if svc_name:
                backend_services.add(svc_name)

    # Also check defaultBackend
    default_backend = spec.get("defaultBackend", {})
    default_svc = default_backend.get("service", {})
    default_svc_name = default_svc.get("name")
    if default_svc_name:
        backend_services.add(default_svc_name)

    endpoint = hosts[0] if hosts else None

    node = {
        "name": name,
        "display_name": f"ingress/{name} ({namespace})",
        "resource_type": "load_balancer",
        "sub_type": "ingress",
        "provider": provider,
        "region": region,
        "cluster_name": cluster_name,
        "namespace": namespace,
        "metadata": {
            "kind": "Ingress",
            "hosts": hosts,
            "labels": metadata.get("labels", {}),
        },
    }
    if endpoint:
        node["endpoint"] = endpoint

    return node, list(backend_services)


# =========================================================================
# Edge Generation
# =========================================================================


def _build_relationships(ingress_backends, service_nodes, workload_nodes, cluster_name):
    """Build dependency edges between Kubernetes resources.

    Generates three types of edges:
        1. Ingress -> Service (load_balancer dependency)
        2. Service -> Deployment/StatefulSet/DaemonSet (http dependency, via selector matching)
        3. Workload -> Cluster (network dependency)

    Args:
        ingress_backends: List of (ingress_name, namespace, backend_svc_names).
        service_nodes: List of service node dicts.
        workload_nodes: List of workload node dicts.
        cluster_name: Name of the parent cluster.

    Returns:
        List of relationship dicts.
    """
    relationships = []

    # Index services by (namespace, k8s_name) for lookup
    # k8s_name is the original K8s name before svc/ prefixing
    svc_by_key = {}
    for svc in service_nodes:
        k8s_name = svc.get("metadata", {}).get("k8s_name") or svc.get("name")
        key = (svc.get("namespace"), k8s_name)
        svc_by_key[key] = svc

    # 1. Ingress -> Service edges
    for ingress_name, namespace, backend_svc_names in ingress_backends:
        for svc_name in backend_svc_names:
            svc_node = svc_by_key.get((namespace, svc_name))
            if svc_node:
                relationships.append({
                    "from_service": ingress_name,
                    "to_service": svc_node.get("name"),
                    "dependency_type": "load_balancer",
                    "confidence": K8S_EDGE_CONFIDENCE,
                    "discovered_from": "kubernetes_ingress",
                })

    # 2. Service -> Workload edges (via selector matching)
    for svc in service_nodes:
        selector = svc.get("metadata", {}).get("selector", {})
        if not selector:
            continue
        svc_namespace = svc.get("namespace")
        for workload in workload_nodes:
            if workload.get("namespace") != svc_namespace:
                continue
            match_labels = workload.get("metadata", {}).get("match_labels", {})
            if not match_labels:
                continue
            # Check if all service selector labels match workload labels
            if all(match_labels.get(k) == v for k, v in selector.items()):
                relationships.append({
                    "from_service": svc.get("name"),
                    "to_service": workload.get("name"),
                    "dependency_type": "http",
                    "confidence": K8S_EDGE_CONFIDENCE,
                    "discovered_from": "kubernetes_service_selector",
                })

    # 3. Workload -> Cluster edges
    for workload in workload_nodes:
        relationships.append({
            "from_service": workload.get("name"),
            "to_service": cluster_name,
            "dependency_type": "network",
            "confidence": K8S_EDGE_CONFIDENCE,
            "discovered_from": "kubernetes_cluster_membership",
        })

    return relationships


# =========================================================================
# Per-Cluster Discovery
# =========================================================================


def _discover_cluster(cluster, provider_credentials):
    """Discover internal resources for a single Kubernetes cluster.

    Args:
        cluster: Cluster dict from Phase 1.
        provider_credentials: Provider credential dict.

    Returns:
        Tuple of (nodes_list, relationships_list, errors_list).
    """
    cluster_name = cluster.get("name", "unknown")
    nodes = []
    relationships = []
    errors = []

    # Step 1: Authenticate kubectl to this cluster
    logger.info(f"K8s enrichment: getting credentials for cluster {cluster_name}")
    cred_error = _get_cluster_credentials(cluster, provider_credentials)
    if cred_error:
        error_msg = f"Failed to get credentials for cluster {cluster_name}: {cred_error}"
        logger.warning(error_msg)
        errors.append(error_msg)
        return nodes, relationships, errors

    # Step 2: Fetch all resource types via kubectl
    raw_resources = {}
    for resource_kind, cmd in KUBECTL_COMMANDS.items():
        logger.info(f"K8s enrichment: fetching {resource_kind} from cluster {cluster_name}")
        data, error = _run_json_command(cmd)
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

    # Step 3: Extract nodes
    workload_nodes = []
    service_nodes = []
    ingress_backends = []  # (ingress_name, namespace, [backend_svc_names])

    # Deployments
    for item in raw_resources.get("deployments", []):
        node = _extract_workload_node(item, "Deployment", cluster)
        workload_nodes.append(node)

    # StatefulSets
    for item in raw_resources.get("statefulsets", []):
        node = _extract_workload_node(item, "StatefulSet", cluster)
        workload_nodes.append(node)

    # DaemonSets
    for item in raw_resources.get("daemonsets", []):
        node = _extract_workload_node(item, "DaemonSet", cluster)
        workload_nodes.append(node)

    # Services
    for item in raw_resources.get("services", []):
        node = _extract_service_node(item, cluster)
        service_nodes.append(node)

    # Ingresses
    for item in raw_resources.get("ingresses", []):
        node, backend_svc_names = _extract_ingress_node(item, cluster)
        nodes.append(node)
        namespace = item.get("metadata", {}).get("namespace", "default")
        ingress_backends.append((node["name"], namespace, backend_svc_names))

    nodes.extend(workload_nodes)
    nodes.extend(service_nodes)

    # Step 4: Build edges
    cluster_relationships = _build_relationships(
        ingress_backends, service_nodes, workload_nodes, cluster_name
    )
    relationships.extend(cluster_relationships)

    logger.info(
        f"K8s enrichment for cluster {cluster_name}: "
        f"{len(nodes)} nodes, {len(relationships)} edges"
    )

    return nodes, relationships, errors


# =========================================================================
# Public Entry Point
# =========================================================================


def enrich(user_id, clusters, provider_credentials):
    """Enrich discovered Kubernetes clusters with internal resource details.

    Runs after Phase 1 discovers K8s clusters. For each cluster, authenticates
    and discovers Deployments, StatefulSets, DaemonSets, Services, and
    Ingresses, then builds dependency edges between them.

    Args:
        user_id: The user performing discovery.
        clusters: List of cluster dicts from Phase 1, each containing:
            - name: Cluster name.
            - provider: Cloud provider (gcp, aws, azure).
            - region: Cloud region.
            - zone: Cloud zone (GKE).
            - cloud_resource_id: Original resource ID.
        provider_credentials: Dict of provider-specific credentials
            (project, resource_group, etc.).

    Returns:
        Dict with keys:
            - nodes: List of discovered K8s resource node dicts.
            - relationships: List of dependency edge dicts.
            - errors: List of error message strings.
    """
    all_nodes = []
    all_relationships = []
    all_errors = []

    if not clusters:
        logger.info("K8s enrichment: no clusters to enrich")
        return {"nodes": [], "relationships": [], "errors": []}

    logger.info(
        f"K8s enrichment: enriching {len(clusters)} clusters for user {user_id}"
    )

    for cluster in clusters:
        cluster_name = cluster.get("name", "unknown")
        try:
            nodes, relationships, errors = _discover_cluster(
                cluster, provider_credentials
            )
            all_nodes.extend(nodes)
            all_relationships.extend(relationships)
            all_errors.extend(errors)
        except Exception as e:
            error_msg = (
                f"Unexpected error enriching cluster {cluster_name}: {e}"
            )
            logger.exception(error_msg)
            all_errors.append(error_msg)

    logger.info(
        f"K8s enrichment complete for user {user_id}: "
        f"{len(all_nodes)} nodes, {len(all_relationships)} relationships, "
        f"{len(all_errors)} errors"
    )

    return {
        "nodes": all_nodes,
        "relationships": all_relationships,
        "errors": all_errors,
    }
