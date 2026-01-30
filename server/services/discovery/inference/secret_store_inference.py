"""
Secret Store Inference - Phase 3 connection inference.

Determines which compute services depend on secret stores (AWS Secrets Manager,
AWS SSM Parameter Store, Azure Key Vault, GCP Secret Manager) by analyzing
IAM policies, environment variables, and app settings.
"""

import logging
import re

from services.discovery.inference.node_lookup import find_compute_node

logger = logging.getLogger(__name__)

# AWS IAM actions indicating secret store access
_AWS_SECRET_ACTIONS = {
    "secretsmanager:GetSecretValue",
    "secretsmanager:DescribeSecret",
    "secretsmanager:ListSecrets",
    "secretsmanager:*",
}

_AWS_SSM_ACTIONS = {
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:*",
}

# GCP IAM permissions indicating secret store access
_GCP_SECRET_PERMISSIONS = {
    "secretmanager.versions.access",
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.*",
}

# Azure Key Vault reference pattern in app settings
_KEYVAULT_REF_PATTERN = re.compile(
    r"@Microsoft\.KeyVault\(SecretUri=https://([a-zA-Z0-9\-]+)\.vault\.azure\.net/",
    re.IGNORECASE,
)

# Env var patterns that reference secret store endpoints
_SECRET_ENDPOINT_PATTERNS = [
    re.compile(r"\.secretsmanager\.[a-z0-9\-]+\.amazonaws\.com", re.IGNORECASE),
    re.compile(r"\.ssm\.[a-z0-9\-]+\.amazonaws\.com", re.IGNORECASE),
    re.compile(r"https://([a-zA-Z0-9\-]+)\.vault\.azure\.net", re.IGNORECASE),
    re.compile(r"secretmanager\.googleapis\.com", re.IGNORECASE),
]

# ARN patterns for secret store resources
_SECRETS_MANAGER_ARN = re.compile(r"^arn:aws:secretsmanager:[^:]+:[^:]+:secret:(.+)")
_SSM_PARAM_ARN = re.compile(r"^arn:aws:ssm:[^:]+:[^:]+:parameter/(.+)")


def _find_secret_store_node(graph_nodes, name_hint, sub_type_hint=None):
    """Find a secret store node in the graph.

    Args:
        graph_nodes: List of graph node dicts.
        name_hint: Partial name or identifier to match against.
        sub_type_hint: Optional sub_type to filter on (e.g. 'secrets_manager', 'key_vault').

    Returns:
        Matching node dict, or None.
    """
    name_lower = name_hint.lower() if name_hint else ""
    for node in graph_nodes:
        if node.get("resource_type") != "secret_store":
            continue
        if sub_type_hint and node.get("sub_type") != sub_type_hint:
            continue
        node_name = (node.get("name") or "").lower()
        node_arn = (node.get("arn") or "").lower()
        if name_lower and (name_lower in node_name or name_lower in node_arn):
            return node
    # If no specific match, return any secret store of the right sub_type
    if sub_type_hint:
        for node in graph_nodes:
            if node.get("resource_type") == "secret_store" and node.get("sub_type") == sub_type_hint:
                return node
    return None


def _infer_from_aws_iam(graph_nodes, enrichment_data):
    """Match IAM roles with Secrets Manager or SSM Parameter Store actions.

    Returns list of edge dicts with confidence 0.6.
    """
    edges = []
    iam_policies = enrichment_data.get("iam_policies", [])

    for policy in iam_policies:
        principal_name = policy.get("principal_name", "")
        statements = policy.get("statements", [])

        compute_node = find_compute_node(graph_nodes, principal_name)
        if not compute_node:
            continue

        for statement in statements:
            if statement.get("Effect") != "Allow":
                continue

            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]

            resources = statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]

            # Check for Secrets Manager access
            has_sm_action = any(a in _AWS_SECRET_ACTIONS for a in actions)
            if has_sm_action:
                for resource_arn in resources:
                    match = _SECRETS_MANAGER_ARN.match(resource_arn)
                    secret_name = match.group(1) if match else ""
                    store_node = _find_secret_store_node(
                        graph_nodes, secret_name, sub_type_hint="secrets_manager"
                    )
                    if store_node:
                        edges.append({
                            "from_service": principal_name,
                            "to_service": store_node["name"],
                            "dependency_type": "storage",
                            "confidence": 0.6,
                            "discovered_from": ["iam"],
                            "detail": f"IAM grants Secrets Manager access on {resource_arn}",
                        })

            # Check for SSM Parameter Store access
            has_ssm_action = any(a in _AWS_SSM_ACTIONS for a in actions)
            if has_ssm_action:
                for resource_arn in resources:
                    match = _SSM_PARAM_ARN.match(resource_arn)
                    param_path = match.group(1) if match else ""
                    store_node = _find_secret_store_node(
                        graph_nodes, param_path, sub_type_hint="parameter_store"
                    )
                    if store_node:
                        edges.append({
                            "from_service": principal_name,
                            "to_service": store_node["name"],
                            "dependency_type": "storage",
                            "confidence": 0.6,
                            "discovered_from": ["iam"],
                            "detail": f"IAM grants SSM Parameter Store access on {resource_arn}",
                        })

    return edges


def _infer_from_azure_keyvault(graph_nodes, enrichment_data):
    """Match Azure Key Vault references in app settings.

    Looks for @Microsoft.KeyVault(...) patterns which indicate direct
    Key Vault integration in Azure App Service / Function App settings.

    Returns list of edge dicts with confidence 0.8.
    """
    edges = []
    app_settings = enrichment_data.get("azure_app_settings", {})

    # app_settings: { service_name: { setting_name: setting_value, ... }, ... }
    for service_name, settings in app_settings.items():
        if not isinstance(settings, dict):
            continue
        for setting_name, setting_value in settings.items():
            if not isinstance(setting_value, str):
                continue
            match = _KEYVAULT_REF_PATTERN.search(setting_value)
            if not match:
                continue

            vault_name = match.group(1)
            store_node = _find_secret_store_node(
                graph_nodes, vault_name, sub_type_hint="key_vault"
            )
            if not store_node:
                continue

            edges.append({
                "from_service": service_name,
                "to_service": store_node["name"],
                "dependency_type": "storage",
                "confidence": 0.8,
                "discovered_from": ["env_var"],
                "detail": f"App setting {setting_name} references Key Vault {vault_name}",
            })

    return edges


def _infer_from_gcp_iam(graph_nodes, enrichment_data):
    """Match GCP service accounts with secretmanager.versions.access permission.

    Returns list of edge dicts with confidence 0.6.
    """
    edges = []
    gcp_iam_bindings = enrichment_data.get("gcp_iam_bindings", [])

    for binding in gcp_iam_bindings:
        service_account = binding.get("service_account", "")
        permissions = binding.get("permissions", [])
        if isinstance(permissions, str):
            permissions = [permissions]

        has_secret_perm = any(p in _GCP_SECRET_PERMISSIONS for p in permissions)
        if not has_secret_perm:
            continue

        compute_node = find_compute_node(graph_nodes, service_account)
        if not compute_node:
            continue

        store_node = _find_secret_store_node(
            graph_nodes, "", sub_type_hint="gcp_secret_manager"
        )
        if not store_node:
            continue

        edges.append({
            "from_service": service_account,
            "to_service": store_node["name"],
            "dependency_type": "storage",
            "confidence": 0.6,
            "discovered_from": ["iam"],
            "detail": f"GCP service account has secretmanager access",
        })

    return edges


def _infer_from_env_vars(graph_nodes, enrichment_data):
    """Match environment variables that reference secret store endpoints.

    When an env var references a secret store endpoint, this boosts
    confidence from 0.6 to 0.8 for any existing IAM-based edge,
    or creates a new edge at 0.8 confidence.

    Returns list of edge dicts with confidence 0.8.
    """
    edges = []
    env_vars = enrichment_data.get("env_vars", {})

    for service_name, variables in env_vars.items():
        if not isinstance(variables, dict):
            continue
        for var_name, var_value in variables.items():
            if not isinstance(var_value, str):
                continue

            for pattern in _SECRET_ENDPOINT_PATTERNS:
                match = pattern.search(var_value)
                if not match:
                    continue

                # Determine which type of secret store
                sub_type = None
                if "secretsmanager" in var_value.lower():
                    sub_type = "secrets_manager"
                elif "ssm" in var_value.lower():
                    sub_type = "parameter_store"
                elif "vault.azure.net" in var_value.lower():
                    sub_type = "key_vault"
                elif "secretmanager.googleapis" in var_value.lower():
                    sub_type = "gcp_secret_manager"

                store_node = _find_secret_store_node(graph_nodes, "", sub_type_hint=sub_type)
                if not store_node:
                    continue

                edges.append({
                    "from_service": service_name,
                    "to_service": store_node["name"],
                    "dependency_type": "storage",
                    "confidence": 0.8,
                    "discovered_from": ["env_var"],
                    "detail": f"Env var {var_name} references secret store endpoint",
                })
                break  # One match per var is enough

    return edges


def infer(user_id, graph_nodes, enrichment_data):
    """Run secret store inference.

    Determines which compute services depend on secret stores by analyzing
    IAM policies, Azure Key Vault references, GCP IAM bindings, and
    environment variables.

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of discovered graph node dicts.
        enrichment_data: Dict of enrichment data from Phase 2.

    Returns:
        List of dependency edge dicts.
    """
    edges = []

    edges.extend(_infer_from_aws_iam(graph_nodes, enrichment_data))
    edges.extend(_infer_from_azure_keyvault(graph_nodes, enrichment_data))
    edges.extend(_infer_from_gcp_iam(graph_nodes, enrichment_data))
    edges.extend(_infer_from_env_vars(graph_nodes, enrichment_data))

    logger.info(
        "Secret store inference for user %s: %d edges (iam=%d, env_var=%d)",
        user_id,
        len(edges),
        sum(1 for e in edges if "iam" in e["discovered_from"]),
        sum(1 for e in edges if "env_var" in e["discovered_from"]),
    )

    return edges
