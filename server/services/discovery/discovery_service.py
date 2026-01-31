"""
Discovery Service - Orchestrates the 3-phase discovery pipeline.
Phase 1: Bulk Asset Discovery (parallel per provider)
Phase 2: Detail Enrichment (sequential per resource type)
Phase 3: Connection Inference (all 11 methods)
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.discovery.graph_writer import write_services, write_dependencies
from services.discovery.providers import (
    gcp_asset_discovery,
    aws_asset_discovery,
    azure_asset_discovery,
    ovh_discovery,
    scaleway_discovery,
    tailscale_discovery,
)
from services.discovery.enrichment import (
    kubernetes_enrichment,
    aws_enrichment,
    azure_enrichment,
    serverless_enrichment,
)
from services.discovery.inference.connection_inference import run_all_inference

logger = logging.getLogger(__name__)

# Map provider names to discovery modules
PROVIDER_MODULES = {
    "gcp": gcp_asset_discovery,
    "aws": aws_asset_discovery,
    "azure": azure_asset_discovery,
    "ovh": ovh_discovery,
    "scaleway": scaleway_discovery,
    "tailscale": tailscale_discovery,
}


def _setup_provider_env(provider_name, user_id, credentials):
    """Build an isolated subprocess env dict for a cloud provider.

    Uses the same credential-setup functions as the chatbot's cloud_exec_tool
    so that CLI commands inside the worker container are properly authenticated.

    Args:
        provider_name: One of gcp, aws, azure, ovh, scaleway, tailscale.
        user_id: The user ID performing discovery.
        credentials: Original credentials dict (may contain project_id, etc.).

    Returns:
        (env_dict_or_None, updated_credentials_dict)
        env_dict is passed to subprocess.run(env=...).
        updated_credentials may contain extra keys (e.g. Tailscale api_key).
    """
    from chat.backend.agent.tools.cloud_exec_tool import (
        setup_gcp_environment_isolated,
        setup_aws_environment_isolated,
        setup_azure_environment_isolated,
        setup_ovh_environment_isolated,
        setup_scaleway_environment_isolated,
        setup_tailscale_environment_isolated,
    )

    try:
        if provider_name == "gcp":
            # Use the first project_id for SA token generation (any project works
            # since the SA has cross-project access after post-auth setup).
            project_ids = credentials.get("project_ids", [])
            selected = project_ids[0] if project_ids else credentials.get("project_id")
            success, resolved_project, _auth_type, env = setup_gcp_environment_isolated(
                user_id, selected_project_id=selected, provider_preference="gcp"
            )
            if success and env:
                return env, credentials  # credentials already has project_ids

        elif provider_name == "aws":
            success, _region, _auth_type, env = setup_aws_environment_isolated(user_id)
            if success and env:
                # Build credentials dict from env so aws_asset_discovery._build_env works
                creds = {
                    "access_key_id": env.get("AWS_ACCESS_KEY_ID", ""),
                    "secret_access_key": env.get("AWS_SECRET_ACCESS_KEY", ""),
                    "session_token": env.get("AWS_SESSION_TOKEN"),
                    "region": env.get("AWS_DEFAULT_REGION", "us-east-1"),
                }
                return None, creds  # AWS provider builds its own env from credentials

        elif provider_name == "azure":
            subscription_id = credentials.get("subscription_id")
            result = setup_azure_environment_isolated(user_id, subscription_id=subscription_id)
            success, resolved_sub, _auth_type, env, auth_command = result
            if success and env:
                # Build credentials dict from env so azure_asset_discovery._build_env works
                creds = {
                    "tenant_id": env.get("AZURE_TENANT_ID", ""),
                    "client_id": env.get("AZURE_CLIENT_ID", ""),
                    "client_secret": env.get("AZURE_CLIENT_SECRET", ""),
                    "subscription_id": resolved_sub or subscription_id,
                }
                return None, creds  # Azure provider builds its own env from credentials

        elif provider_name == "ovh":
            success, _project, _auth_type, env = setup_ovh_environment_isolated(user_id)
            if success and env:
                return env, credentials

        elif provider_name == "scaleway":
            success, _project, _auth_type, env = setup_scaleway_environment_isolated(user_id)
            if success and env:
                return env, credentials

        elif provider_name == "tailscale":
            success, tailnet, _auth_type, env = setup_tailscale_environment_isolated(user_id)
            if success and env:
                creds = {
                    "api_key": env.get("TAILSCALE_ACCESS_TOKEN", ""),
                    "tailnet": env.get("TAILSCALE_TAILNET", tailnet or "-"),
                }
                return None, creds  # Tailscale uses REST API, not subprocess

    except Exception as e:
        logger.error(f"[Discovery] Failed to setup {provider_name} environment for user {user_id}: {e}")

    logger.warning(f"[Discovery] Could not obtain credentials for {provider_name}, user {user_id}")
    return None, credentials


def run_discovery_for_user(user_id, connected_providers):
    """Run the full 3-phase discovery pipeline for a single user.

    Args:
        user_id: The user ID.
        connected_providers: Dict mapping provider name to credentials dict.
            e.g. {"gcp": {"project_id": "..."}, "aws": {"access_key_id": "..."}}

    Returns:
        Summary dict with counts and timing.
    """
    start_time = time.time()
    summary = {
        "user_id": user_id,
        "phase1_nodes": 0,
        "phase1_relationships": 0,
        "phase2_nodes": 0,
        "phase2_relationships": 0,
        "phase3_edges": 0,
        "errors": [],
    }

    # =====================================================================
    # Phase 1: Bulk Asset Discovery (parallel per provider)
    # =====================================================================
    logger.info(f"[Discovery] Phase 1 starting for user {user_id} with providers: {list(connected_providers.keys())}")
    all_nodes = []
    all_phase1_relationships = []
    gcp_relationships_raw = []

    # Build authenticated environments for each provider (sequential — credential
    # setup may involve token refresh, STS calls, etc.)
    provider_envs = {}
    for provider_name, credentials in connected_providers.items():
        env, updated_creds = _setup_provider_env(provider_name, user_id, credentials)
        provider_envs[provider_name] = (env, updated_creds)
        connected_providers[provider_name] = updated_creds

    with ThreadPoolExecutor(max_workers=len(connected_providers)) as executor:
        futures = {}
        for provider_name, credentials in connected_providers.items():
            module = PROVIDER_MODULES.get(provider_name)
            if not module:
                logger.warning(f"[Discovery] Unknown provider: {provider_name}")
                continue
            env, creds = provider_envs.get(provider_name, (None, credentials))
            futures[executor.submit(module.discover, user_id, creds, env)] = provider_name

        for future in as_completed(futures):
            provider_name = futures[future]
            try:
                result = future.result()
                nodes = result.get("nodes", [])
                relationships = result.get("relationships", [])
                errors = result.get("errors", [])

                all_nodes.extend(nodes)
                all_phase1_relationships.extend(relationships)

                # Store raw GCP relationships for Phase 3 inference
                if provider_name == "gcp" and result.get("raw_relationships"):
                    gcp_relationships_raw = result["raw_relationships"]

                if errors:
                    summary["errors"].extend(errors)

                logger.info(f"[Discovery] Phase 1 {provider_name}: {len(nodes)} nodes, {len(relationships)} relationships")
            except Exception as e:
                error_msg = f"Phase 1 {provider_name} failed: {str(e)}"
                logger.error(f"[Discovery] {error_msg}")
                summary["errors"].append(error_msg)

    # Write Phase 1 nodes to Memgraph
    summary["phase1_nodes"] = write_services(user_id, all_nodes)

    # Write Phase 1 relationships to Memgraph
    if all_phase1_relationships:
        summary["phase1_relationships"] = write_dependencies(user_id, all_phase1_relationships)

    logger.info(f"[Discovery] Phase 1 complete: {summary['phase1_nodes']} nodes, {summary['phase1_relationships']} relationships")

    # =====================================================================
    # Phase 2: Detail Enrichment (sequential)
    # =====================================================================
    logger.info(f"[Discovery] Phase 2 starting for user {user_id}")
    enrichment_data = {}

    # Kubernetes enrichment (for discovered clusters)
    k8s_clusters = [n for n in all_nodes if n.get("resource_type") == "kubernetes_cluster"]
    if k8s_clusters:
        try:
            k8s_result = kubernetes_enrichment.enrich(user_id, k8s_clusters, connected_providers)
            k8s_nodes = k8s_result.get("nodes", [])
            k8s_rels = k8s_result.get("relationships", [])
            if k8s_nodes:
                summary["phase2_nodes"] += write_services(user_id, k8s_nodes)
                all_nodes.extend(k8s_nodes)
            if k8s_rels:
                summary["phase2_relationships"] += write_dependencies(user_id, k8s_rels)
            if k8s_result.get("errors"):
                summary["errors"].extend(k8s_result["errors"])
            logger.info(f"[Discovery] Phase 2 K8s: {len(k8s_nodes)} nodes, {len(k8s_rels)} relationships")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 K8s enrichment failed: {e}")
            summary["errors"].append(f"K8s enrichment failed: {str(e)}")

    # AWS enrichment
    if "aws" in connected_providers:
        aws_nodes = [n for n in all_nodes if n.get("provider") == "aws"]
        try:
            aws_result = aws_enrichment.enrich(user_id, aws_nodes, connected_providers["aws"])
            enrichment_data.update(aws_result.get("enrichment_data", {}))
            if aws_result.get("errors"):
                summary["errors"].extend(aws_result["errors"])
            logger.info(f"[Discovery] Phase 2 AWS enrichment complete")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 AWS enrichment failed: {e}")
            summary["errors"].append(f"AWS enrichment failed: {str(e)}")

    # Azure enrichment
    if "azure" in connected_providers:
        azure_nodes = [n for n in all_nodes if n.get("provider") == "azure"]
        try:
            azure_result = azure_enrichment.enrich(user_id, azure_nodes, connected_providers["azure"])
            enrichment_data.update(azure_result.get("enrichment_data", {}))
            if azure_result.get("errors"):
                summary["errors"].extend(azure_result["errors"])
            logger.info(f"[Discovery] Phase 2 Azure enrichment complete")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 Azure enrichment failed: {e}")
            summary["errors"].append(f"Azure enrichment failed: {str(e)}")

    # Serverless enrichment
    serverless_nodes = [n for n in all_nodes if n.get("resource_type") == "serverless_function"]
    if serverless_nodes:
        try:
            serverless_result = serverless_enrichment.enrich(user_id, serverless_nodes, connected_providers)
            enrichment_data["env_vars"] = serverless_result.get("env_vars", {})
            if serverless_result.get("errors"):
                summary["errors"].extend(serverless_result["errors"])
            logger.info(f"[Discovery] Phase 2 Serverless enrichment complete")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 Serverless enrichment failed: {e}")
            summary["errors"].append(f"Serverless enrichment failed: {str(e)}")

    # Add GCP relationships for Phase 3 inference
    if gcp_relationships_raw:
        enrichment_data["gcp_relationships"] = gcp_relationships_raw

    logger.info(f"[Discovery] Phase 2 complete: {summary['phase2_nodes']} new nodes, {summary['phase2_relationships']} relationships")

    # =====================================================================
    # Phase 3: Connection Inference
    # =====================================================================
    logger.info(f"[Discovery] Phase 3 starting for user {user_id}")

    try:
        inferred_edges = run_all_inference(user_id, all_nodes, enrichment_data)
        summary["phase3_edges"] = write_dependencies(user_id, inferred_edges)
        logger.info(f"[Discovery] Phase 3 complete: {summary['phase3_edges']} inferred edges")
    except Exception as e:
        logger.error(f"[Discovery] Phase 3 inference failed: {e}")
        summary["errors"].append(f"Connection inference failed: {str(e)}")

    # =====================================================================
    # Summary
    # =====================================================================
    elapsed = time.time() - start_time
    summary["elapsed_seconds"] = round(elapsed, 1)
    total_nodes = summary["phase1_nodes"] + summary["phase2_nodes"]
    total_edges = summary["phase1_relationships"] + summary["phase2_relationships"] + summary["phase3_edges"]
    logger.info(
        f"[Discovery] Complete for user {user_id}: "
        f"{total_nodes} nodes, {total_edges} edges, "
        f"{len(summary['errors'])} errors, {elapsed:.1f}s"
    )
    return summary
