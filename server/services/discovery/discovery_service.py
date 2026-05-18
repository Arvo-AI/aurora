"""
Discovery Service - Orchestrates the 3-phase discovery pipeline.
Phase 1: Bulk Asset Discovery (parallel per provider)
Phase 2: Detail Enrichment (sequential per resource type)
Phase 3: Connection Inference (all 11 methods)
"""

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.log_sanitizer import safe_provider, hash_for_log
from services.discovery.graph_writer import write_services, write_dependencies
from services.discovery.providers import (
    gcp_asset_discovery,
    aws_asset_discovery,
    azure_asset_discovery,
    ovh_discovery,
    scaleway_discovery,
    tailscale_discovery,
    kubectl_discovery,
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
    "kubectl": kubectl_discovery,
}


def _setup_provider_env(provider_name, user_id, credentials):
    """Build an isolated subprocess env dict for a cloud provider.

    Uses the same credential-setup functions as the chatbot's cloud_exec_tool
    so that CLI commands inside the worker container are properly authenticated.

    Args:
        provider_name: One of gcp, aws, azure, ovh, scaleway, tailscale.
        user_id: The user ID performing discovery (used for RLS / error tracking).
        credentials: Original credentials dict (may contain project_id,
            owner_id, etc.).  When an ``owner_id`` key is present it is used
            as the credential-lookup identity so org-shared connectors are
            resolved against the correct Vault path, regardless of which org
            member triggered discovery.

    Returns:
        (env_dict_or_None, updated_credentials_dict)
        env_dict is passed to subprocess.run(env=...).
        updated_credentials may contain extra keys (e.g. Tailscale api_key).
    """
    from chat.backend.agent.tools.cloud_exec_tool import (
        setup_gcp_environment_isolated,
        setup_aws_environment_isolated,
        setup_aws_environments_all_accounts,
        setup_azure_environment_isolated,
        setup_ovh_environment_isolated,
        setup_scaleway_environment_isolated,
        setup_tailscale_environment_isolated,
    )

    # Use the per-provider credential owner when available so org-shared
    # connectors resolve against the correct Vault secret path.
    auth_user_id = credentials.pop("owner_id", None) or user_id

    # kubectl uses the chatbot internal API, no subprocess env needed
    if provider_name == "kubectl":
        return None, credentials

    try:
        if provider_name == "gcp":
            # Use the first project_id for SA token generation (any project works
            # since the SA has cross-project access after post-auth setup).
            project_ids = credentials.get("project_ids", [])
            selected = project_ids[0] if project_ids else credentials.get("project_id")
            success, resolved_project, _auth_type, env = setup_gcp_environment_isolated(
                auth_user_id, selected_project_id=selected, provider_preference="gcp"
            )
            if success and env:
                return env, credentials  # credentials already has project_ids

        elif provider_name == "aws":
            # Try multi-account first; fall back to single-account
            account_envs = setup_aws_environments_all_accounts(auth_user_id)
            if account_envs and len(account_envs) > 1:
                creds = {
                    "_multi_account": True,
                    "_account_envs": account_envs,
                }
                return None, creds

            # Single account (or first of multi)
            if account_envs:
                acct = account_envs[0]
                creds = {
                    "access_key_id": acct["credentials"]["accessKeyId"],
                    "secret_access_key": acct["credentials"]["secretAccessKey"],
                    "session_token": acct["credentials"]["sessionToken"],
                    "region": acct["region"],
                }
                return None, creds

            # Legacy fallback
            success, _region, _auth_type, env = setup_aws_environment_isolated(auth_user_id)
            if success and env:
                creds = {
                    "access_key_id": env.get("AWS_ACCESS_KEY_ID", ""),
                    "secret_access_key": env.get("AWS_SECRET_ACCESS_KEY", ""),
                    "session_token": env.get("AWS_SESSION_TOKEN"),
                    "region": env.get("AWS_DEFAULT_REGION", "us-east-1"),
                }
                return None, creds

        elif provider_name == "azure":
            subscription_id = credentials.get("subscription_id")
            result = setup_azure_environment_isolated(auth_user_id, subscription_id=subscription_id)
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
            success, _project, _auth_type, env = setup_ovh_environment_isolated(auth_user_id)
            if success and env:
                return env, credentials

        elif provider_name == "scaleway":
            success, _project, _auth_type, env = setup_scaleway_environment_isolated(auth_user_id)
            if success and env:
                return env, credentials

        elif provider_name == "tailscale":
            success, tailnet, _auth_type, env = setup_tailscale_environment_isolated(auth_user_id)
            if success and env:
                creds = {
                    "api_key": env.get("TAILSCALE_ACCESS_TOKEN", ""),
                    "tailnet": env.get("TAILSCALE_TAILNET", tailnet or "-"),
                }
                return None, creds  # Tailscale uses REST API, not subprocess

    except Exception as e:
        logger.exception(
            "[Discovery] Failed to setup %s environment for user_hash=%s (auth_as_hash=%s), error_type=%s",
            safe_provider(provider_name),
            hash_for_log(user_id),
            hash_for_log(auth_user_id),
            type(e).__name__,
        )

    logger.warning(
        "[Discovery] Could not obtain credentials for %s, user_hash=%s",
        safe_provider(provider_name),
        hash_for_log(user_id),
    )
    return None, credentials


def run_discovery_for_user(user_id, connected_providers):
    """Run the full 3-phase discovery pipeline for a single user.

    Args:
        user_id: The user ID.
        connected_providers: Dict mapping provider name to credentials dict.
            e.g. {"gcp": {"project_id": "..."}, "aws": {"access_key_id": "..."}}

    Returns:
        Summary dict with counts, timing, and a ``provider_errors`` key that
        maps each provider name to its list of error strings. This allows
        callers to do accurate per-provider failure tracking without guessing
        from error message text.
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
        "provider_errors": {},
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

            # Multi-account AWS: fan out via discover_all_accounts
            if provider_name == "aws" and isinstance(creds, dict) and creds.get("_multi_account"):
                from services.discovery.providers.aws_asset_discovery import discover_all_accounts
                account_envs = creds["_account_envs"]
                futures[executor.submit(discover_all_accounts, user_id, account_envs)] = provider_name
            else:
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
                    summary["provider_errors"].setdefault(provider_name, []).extend(errors)

                logger.info(f"[Discovery] Phase 1 {provider_name}: {len(nodes)} nodes, {len(relationships)} relationships")
            except Exception as e:
                error_msg = f"Phase 1 {provider_name} failed: {str(e)}"
                logger.error(f"[Discovery] {error_msg}")
                summary["errors"].append(error_msg)
                summary["provider_errors"].setdefault(provider_name, []).append(error_msg)

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

    # Kubernetes enrichment (for cloud-managed clusters only — kubectl clusters
    # already have their internals discovered in Phase 1)
    k8s_clusters = [n for n in all_nodes if n.get("resource_type") == "kubernetes_cluster" and n.get("provider") != "kubectl"]
    if k8s_clusters:
        try:
            k8s_result = kubernetes_enrichment.enrich(
                user_id, k8s_clusters, connected_providers,
                provider_envs={p: env for p, (env, _) in provider_envs.items() if env is not None},
            )
            k8s_nodes = k8s_result.get("nodes", [])
            k8s_rels = k8s_result.get("relationships", [])
            if k8s_nodes:
                summary["phase2_nodes"] += write_services(user_id, k8s_nodes)
                all_nodes.extend(k8s_nodes)
            if k8s_rels:
                summary["phase2_relationships"] += write_dependencies(user_id, k8s_rels)
            if k8s_result.get("errors"):
                summary["errors"].extend(k8s_result["errors"])
                # Attribute each error to its provider via substring matching.
                # kubernetes_enrichment returns plain strings in its errors list.
                for err in k8s_result["errors"]:
                    attributed = False
                    err_str = err if isinstance(err, str) else str(err)
                    # Substring match: a cluster name like "aws-prod" will
                    # attribute to "aws", which is usually correct (a cluster
                    # named "aws-prod" really is on AWS).  Keep this assumption
                    # in mind if attribution appears wrong in the future.
                    for pname in ("gcp", "aws", "azure"):
                        if pname in connected_providers and pname in err_str.lower():
                            summary["provider_errors"].setdefault(pname, []).append(err_str)
                            attributed = True
                            break
                    if not attributed:
                        summary.setdefault("unknown_provider_errors", []).append(err_str)
            stale = k8s_result.get("stale_clusters", [])
            if stale:
                logger.warning("[Discovery] Stale K8s clusters for user_hash=%s: %s", hash_for_log(user_id), stale)
                summary.setdefault("stale_clusters", []).extend(stale)
            logger.info(f"[Discovery] Phase 2 K8s: {len(k8s_nodes)} nodes, {len(k8s_rels)} relationships")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 K8s enrichment failed: {e}")
            summary["errors"].append(f"K8s enrichment failed: {str(e)}")

    # AWS / Azure enrichment (identical pattern: filter nodes, enrich, collect data)
    provider_enrichments = {
        "aws": aws_enrichment,
        "azure": azure_enrichment,
    }
    for provider_name, enrichment_module in provider_enrichments.items():
        if provider_name not in connected_providers:
            continue
        provider_nodes = [n for n in all_nodes if n.get("provider") == provider_name]
        try:
            result = enrichment_module.enrich(user_id, provider_nodes, connected_providers[provider_name])
            enrichment_data.update(result.get("enrichment_data", {}))
            if result.get("errors"):
                summary["errors"].extend(result["errors"])
                summary["provider_errors"].setdefault(provider_name, []).extend(result["errors"])
            logger.info(f"[Discovery] Phase 2 {provider_name.upper()} enrichment complete")
        except Exception as e:
            label = provider_name.upper()
            logger.error(f"[Discovery] Phase 2 {label} enrichment failed: {e}")
            error_msg = f"{label} enrichment failed: {str(e)}"
            summary["errors"].append(error_msg)
            summary["provider_errors"].setdefault(provider_name, []).append(error_msg)

    # Serverless enrichment
    serverless_nodes = [n for n in all_nodes if n.get("resource_type") == "serverless_function"]
    if serverless_nodes:
        try:
            # Pass provider_envs so GCP gcloud calls get the isolated auth env
            serverless_result = serverless_enrichment.enrich(
                user_id, serverless_nodes, connected_providers,
                provider_envs={p: env for p, (env, _) in provider_envs.items() if env is not None},
            )
            enrichment_data["env_vars"] = serverless_result.get("env_vars", {})
            if serverless_result.get("errors"):
                summary["errors"].extend(serverless_result["errors"])
                # Serverless errors are provider-prefixed (e.g. "aws/fn-name"),
                # attribute via substring match against the error string.
                for err in serverless_result["errors"]:
                    err_str = err if isinstance(err, str) else str(err)
                    for pname in ("gcp", "aws", "azure"):
                        if pname in connected_providers and pname in err_str.lower():
                            summary["provider_errors"].setdefault(pname, []).append(err_str)
                            break
            logger.info("[Discovery] Phase 2 Serverless enrichment complete")
        except Exception as e:
            logger.error(f"[Discovery] Phase 2 Serverless enrichment failed: {e}")
            error_msg = f"Serverless enrichment failed: {str(e)}"
            summary["errors"].append(error_msg)
            # Attribute only to providers actually present in this serverless run.
            affected_providers = {
                n.get("provider")
                for n in serverless_nodes
                if n.get("provider") in connected_providers
            }
            for pname in affected_providers:
                summary["provider_errors"].setdefault(pname, []).append(error_msg)

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

    # Clean up any ephemeral gcloud temp directories created during this run.
    for env, _ in provider_envs.values():
        tmpdir = env.get("_gcloud_tmpdir") if isinstance(env, dict) else None
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return summary
