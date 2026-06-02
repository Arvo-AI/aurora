"""
Kubernetes Internal Discovery Enrichment (Phase 2).

Runs after Phase 1 discovers K8s clusters (GKE, EKS, AKS). For each cluster,
authenticates via the appropriate CLI, then uses kubectl to discover internal
workloads (Deployments, StatefulSets, DaemonSets, Services, Ingresses) and
maps them into normalized graph nodes and dependency edges.
"""

import json
import logging
import os

from services.discovery.enrichment.cli_utils import run_cli_command
from services.discovery.resource_mapper import infer_type_from_image


def _extract_aws_account_id(arn: str) -> str | None:
    """Extract AWS account ID from an ARN string, or None if invalid."""
    if not arn.startswith("arn:aws"):
        return None
    parts = arn.split(":")
    return parts[4] if len(parts) >= 5 and parts[4] else None

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


def _run_json_command(args, env, timeout=KUBECTL_TIMEOUT):
    """Run a CLI command expecting JSON output.

    Returns:
        Tuple of (parsed_json, error_string_or_None).
    """
    stdout, error = run_cli_command(args, env, timeout=timeout)
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


def _get_gcp_cluster_credentials(cluster, provider_credentials, provider_envs):
    """Authenticate kubectl to a GKE cluster."""
    cluster_name = cluster.get("name", "")
    region = cluster.get("region", "")
    zone = cluster.get("zone", "")
    gcp_creds = provider_credentials.get("gcp", provider_credentials)
    project = (
        cluster.get("metadata", {}).get("project_id")
        or cluster.get("project", "")
        or gcp_creds.get("project")
        or (gcp_creds.get("project_ids") or [None])[0]
    )
    if not (zone or region):
        return f"GKE cluster {cluster_name}: missing zone/region"
    args = [
        "gcloud", "container", "clusters", "get-credentials",
        cluster_name,
    ]
    if zone:
        args.extend(["--zone", zone])
    else:
        args.extend(["--region", region])
    if project:
        args.extend(["--project", project])
    gcp_env = provider_envs.get("gcp") or {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    _, error = run_cli_command(args, env=gcp_env, timeout=CREDENTIALS_TIMEOUT)
    return error


def _get_aws_cluster_credentials(cluster, provider_envs):
    """Authenticate kubectl to an EKS cluster."""
    cluster_name = cluster.get("name", "")
    region = cluster.get("region", "")
    if not region:
        return f"EKS cluster {cluster_name}: missing region"
    args = [
        "aws", "eks", "update-kubeconfig",
        "--name", cluster_name,
        "--region", region,
    ]
    multi_envs = provider_envs.get("_aws_multi", {})
    aws_env = provider_envs.get("aws")
    if multi_envs:
        arn = cluster.get("cloud_resource_id", "")
        acct_id = _extract_aws_account_id(arn)
        aws_env = multi_envs.get(acct_id, aws_env)
    aws_env = aws_env or {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    _, error = run_cli_command(args, env=aws_env, timeout=CREDENTIALS_TIMEOUT)
    return error


def _get_azure_cluster_credentials(cluster, provider_credentials, provider_envs):
    """Authenticate kubectl to an AKS cluster."""
    cluster_name = cluster.get("name", "")
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
    azure_env = provider_envs.get("azure") or {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    _, error = run_cli_command(args, env=azure_env, timeout=CREDENTIALS_TIMEOUT)
    return error


def _get_cluster_credentials(cluster, provider_credentials, provider_envs=None):
    """Authenticate kubectl to a cluster using the appropriate cloud CLI.

    Args:
        cluster: Cluster dict from Phase 1 with name, provider, region, zone,
                 cloud_resource_id.
        provider_credentials: Dict of provider credentials (may include
                              project, resource_group, etc.).
        provider_envs: Optional dict mapping provider name to the isolated
                       subprocess env dict built during Phase 1.

    Returns:
        Error string or None on success.
    """
    if provider_envs is None:
        provider_envs = {}

    provider = cluster.get("provider", "").lower()

    if provider == "gcp":
        return _get_gcp_cluster_credentials(cluster, provider_credentials, provider_envs)
    elif provider == "aws":
        return _get_aws_cluster_credentials(cluster, provider_envs)
    elif provider == "azure":
        return _get_azure_cluster_credentials(cluster, provider_credentials, provider_envs)
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


_STALE_CLUSTER_FRAGMENTS = (
    # EKS cluster deleted from AWS
    "ResourceNotFoundException",
    "No cluster found for name",
    # GKE cluster deleted or project removed
    "NOT_FOUND",
    "cluster not found",
    # AKS cluster deleted
    "ResourceNotFound",
    "(ResourceNotFound)",
)


def _is_stale_cluster_error(error_msg: str) -> bool:
    """Return True when error_msg indicates the cluster no longer exists.

    NOTE: This function is only intended to be called on stderr from the
    ``get-credentials`` command (via ``_get_cluster_credentials``).  The
    fragments below (e.g. "NOT_FOUND", "cluster not found") are scoped to
    that context and may produce false positives if applied to kubectl fetch
    errors from other call sites.
    """
    lower = error_msg.lower()
    return any(frag.lower() in lower for frag in _STALE_CLUSTER_FRAGMENTS)


def _resolve_kubectl_env(cluster, provider_envs):
    """Return the minimal subprocess env to use for kubectl calls on a cluster.

    Selects the account-specific env for multi-account EKS; falls back to the
    provider env; guarantees a non-None minimal env so subprocess never inherits
    the full server environment.
    """
    provider = cluster.get("provider", "").lower()
    kubectl_env = provider_envs.get(provider)
    if provider == "aws" and provider_envs.get("_aws_multi"):
        multi_envs = provider_envs["_aws_multi"]
        arn = cluster.get("cloud_resource_id", "")
        acct_id = _extract_aws_account_id(arn)
        kubectl_env = multi_envs.get(acct_id, kubectl_env)
    if kubectl_env is None:
        kubectl_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    return kubectl_env


def _fetch_raw_resources(cluster_name, kubectl_env):
    """Run all KUBECTL_COMMANDS and return (raw_resources, errors)."""
    raw_resources = {}
    errors = []
    for resource_kind, cmd in KUBECTL_COMMANDS.items():
        logger.info("K8s enrichment: fetching %s from cluster %s", resource_kind, cluster_name)
        data, error = _run_json_command(cmd, kubectl_env)
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
    return raw_resources, errors


def _extract_cluster_nodes(raw_resources, cluster):
    """Extract workload, service, and ingress nodes from raw kubectl output.

    Returns (nodes, workload_nodes, service_nodes, ingress_backends).
    """
    workload_nodes = []
    service_nodes = []
    ingress_backends = []
    nodes = []

    workload_kinds = [
        ("deployments", "Deployment"),
        ("statefulsets", "StatefulSet"),
        ("daemonsets", "DaemonSet"),
    ]
    for resource_key, kind in workload_kinds:
        for item in raw_resources.get(resource_key, []):
            workload_nodes.append(_extract_workload_node(item, kind, cluster))

    for item in raw_resources.get("services", []):
        service_nodes.append(_extract_service_node(item, cluster))

    for item in raw_resources.get("ingresses", []):
        node, backend_svc_names = _extract_ingress_node(item, cluster)
        nodes.append(node)
        namespace = item.get("metadata", {}).get("namespace", "default")
        ingress_backends.append((node["name"], namespace, backend_svc_names))

    return nodes, workload_nodes, service_nodes, ingress_backends


def _discover_cluster(cluster, provider_credentials, provider_envs=None):
    """Discover internal resources for a single Kubernetes cluster.

    Args:
        cluster: Cluster dict from Phase 1.
        provider_credentials: Provider credential dict.
        provider_envs: Optional dict mapping provider name to the isolated
                       subprocess env dict built during Phase 1.

    Returns:
        Tuple of (nodes_list, relationships_list, errors_list, stale_bool).
        stale_bool is True when the cluster was not found in the cloud provider
        and the caller should consider removing the connection record.
    """
    if provider_envs is None:
        provider_envs = {}

    cluster_name = cluster.get("name", "unknown")
    nodes = []
    relationships = []
    errors = []

    # Step 1: Authenticate kubectl to this cluster
    logger.info("K8s enrichment: getting credentials for cluster %s", cluster_name)
    cred_error = _get_cluster_credentials(cluster, provider_credentials, provider_envs)
    if cred_error:
        if _is_stale_cluster_error(cred_error):
            logger.warning(
                "K8s enrichment: cluster %s no longer exists in cloud provider — skipping",
                cluster_name,
            )
            return nodes, relationships, [], True
        error_msg = f"Failed to get credentials for cluster {cluster_name}: {cred_error}"
        logger.warning(error_msg)
        errors.append(error_msg)
        return nodes, relationships, errors, False

    # Step 2: Fetch all resource types via kubectl.
    # The provider env must be forwarded so gke-gcloud-auth-plugin (invoked by
    # kubectl on every call) can authenticate using CLOUDSDK_AUTH_ACCESS_TOKEN
    # and CLOUDSDK_CONFIG rather than looking for a gcloud account that doesn't
    # exist in the container's default shell.
    kubectl_env = _resolve_kubectl_env(cluster, provider_envs)

    raw_resources, fetch_errors = _fetch_raw_resources(cluster_name, kubectl_env)
    errors.extend(fetch_errors)

    # Step 3: Extract nodes
    ingress_nodes, workload_nodes, service_nodes, ingress_backends = _extract_cluster_nodes(
        raw_resources, cluster
    )
    nodes.extend(ingress_nodes)
    nodes.extend(workload_nodes)
    nodes.extend(service_nodes)

    # Step 4: Build edges
    cluster_relationships = _build_relationships(
        ingress_backends, service_nodes, workload_nodes, cluster_name
    )
    relationships.extend(cluster_relationships)

    logger.info(
        "K8s enrichment for cluster %s: %d nodes, %d edges",
        cluster_name, len(nodes), len(relationships),
    )

    return nodes, relationships, errors, False


# =========================================================================
# Public Entry Point
# =========================================================================


def enrich(user_id, clusters, provider_credentials, provider_envs=None):
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
        provider_envs: Optional dict mapping provider name to the isolated
            subprocess env dict built during Phase 1. When provided, GKE/EKS/AKS
            credential commands run with the correct per-user auth context instead
            of the shared container environment.

    Returns:
        Dict with keys:
            - nodes: List of discovered K8s resource node dicts.
            - relationships: List of dependency edge dicts.
            - errors: List of error message strings.
            - stale_clusters: List of cluster names that no longer exist in the
              cloud provider (caller can use this to clean up stale records).
    """
    all_nodes = []
    all_relationships = []
    all_errors = []
    stale_clusters = []

    if provider_envs is None:
        provider_envs = {}

    if not clusters:
        logger.info("K8s enrichment: no clusters to enrich")
        return {"nodes": [], "relationships": [], "errors": [], "stale_clusters": []}

    logger.info(
        f"K8s enrichment: enriching {len(clusters)} clusters for user {user_id}"
    )

    for cluster in clusters:
        cluster_name = cluster.get("name", "unknown")
        try:
            nodes, relationships, errors, is_stale = _discover_cluster(
                cluster, provider_credentials, provider_envs
            )
            if is_stale:
                stale_clusters.append(cluster_name)
                continue
            all_nodes.extend(nodes)
            all_relationships.extend(relationships)
            all_errors.extend(errors)
        except Exception as e:
            error_msg = (
                f"Unexpected error enriching cluster {cluster_name}: {e}"
            )
            logger.exception(error_msg)
            all_errors.append(error_msg)

    if stale_clusters:
        logger.warning(
            "K8s enrichment: %d stale cluster(s) skipped for user %s: %s",
            len(stale_clusters), user_id, stale_clusters,
        )

    logger.info(
        f"K8s enrichment complete for user {user_id}: "
        f"{len(all_nodes)} nodes, {len(all_relationships)} relationships, "
        f"{len(all_errors)} errors, {len(stale_clusters)} stale"
    )

    return {
        "nodes": all_nodes,
        "relationships": all_relationships,
        "errors": all_errors,
        "stale_clusters": stale_clusters,
    }
