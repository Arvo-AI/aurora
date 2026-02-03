"""
Azure Detail Enrichment - Phase 2 discovery enrichment.

Fetches supplementary data that Azure Resource Graph does not provide,
such as NSG security rules, web/function app environment settings, and
DNS zone record sets. Only fetches data for resources that were actually
discovered during Phase 1.

The enrichment data is consumed by Phase 3 inference to build dependency
edges between discovered nodes.
"""

import logging
import os

from services.discovery.enrichment.cli_utils import run_cli_json_command

logger = logging.getLogger(__name__)


def _build_env(credentials):
    """Build environment variables for Azure CLI service principal auth.

    If tenant_id, client_id, and client_secret are all present, sets the
    corresponding AZURE_* environment variables. Otherwise returns a copy
    of the current environment (assumes ``az login`` was already done).
    """
    env = os.environ.copy()

    tenant_id = credentials.get("tenant_id")
    client_id = credentials.get("client_id")
    client_secret = credentials.get("client_secret")

    if tenant_id and client_id and client_secret:
        env["AZURE_TENANT_ID"] = tenant_id
        env["AZURE_CLIENT_ID"] = client_id
        env["AZURE_CLIENT_SECRET"] = client_secret

    return env


def _extract_resource_group(node):
    """Extract the resource group name from a node's metadata or cloud_resource_id."""
    metadata = node.get("metadata", {})
    rg = metadata.get("resource_group")
    if rg:
        return rg

    # Fall back to parsing the cloud_resource_id
    cloud_id = node.get("cloud_resource_id", "")
    parts = cloud_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _collect_nodes_by_type(azure_nodes):
    """Organize Phase 1 nodes by their Azure resource type.

    Returns a dict mapping normalized Azure type strings to lists of
    (name, resource_group) tuples.
    """
    grouped = {}
    for node in azure_nodes:
        azure_type = (node.get("metadata", {}).get("azure_type") or "").lower()
        name = node.get("name", "")
        resource_group = _extract_resource_group(node)
        if azure_type and name and resource_group:
            grouped.setdefault(azure_type, []).append({
                "name": name,
                "resource_group": resource_group,
            })
    return grouped


def _fetch_nsg_rules(nodes_by_type, env):
    """Fetch security rules for each discovered NSG.

    Args:
        nodes_by_type: Dict from _collect_nodes_by_type.
        env: Environment dict for subprocess.

    Returns:
        (nsg_rules_dict, errors_list) where nsg_rules_dict maps
        NSG name to its list of rules.
    """
    nsg_nodes = nodes_by_type.get("microsoft.network/networksecuritygroups", [])
    if not nsg_nodes:
        return {}, []

    nsg_rules = {}
    errors = []

    for nsg in nsg_nodes:
        nsg_name = nsg["name"]
        rg = nsg["resource_group"]
        cmd = [
            "az", "network", "nsg", "rule", "list",
            "--nsg-name", nsg_name,
            "--resource-group", rg,
            "--output", "json",
        ]
        data = run_cli_json_command(cmd, env)
        if data is None:
            errors.append(f"Failed to fetch rules for NSG {nsg_name} in {rg}")
        else:
            nsg_rules[nsg_name] = data
            logger.info("Fetched %d rules for NSG %s", len(data), nsg_name)

    return nsg_rules, errors


def _fetch_app_settings(nodes_by_type, env):
    """Fetch environment/app settings for web apps and function apps.

    Args:
        nodes_by_type: Dict from _collect_nodes_by_type.
        env: Environment dict for subprocess.

    Returns:
        (app_settings_dict, errors_list) where app_settings_dict maps
        app name to its settings list.
    """
    app_settings = {}
    errors = []

    # Web apps (kind=app) and function apps (kind=functionapp) both live
    # under microsoft.web/sites in Azure Resource Graph.
    web_apps = nodes_by_type.get("microsoft.web/sites", [])

    for app in web_apps:
        app_name = app["name"]
        rg = app["resource_group"]

        # Try webapp first; fall back to functionapp if it fails
        webapp_cmd = [
            "az", "webapp", "config", "appsettings", "list",
            "--name", app_name,
            "--resource-group", rg,
            "--output", "json",
        ]
        data = run_cli_json_command(webapp_cmd, env)

        if data is None:
            # Might be a function app
            func_cmd = [
                "az", "functionapp", "config", "appsettings", "list",
                "--name", app_name,
                "--resource-group", rg,
                "--output", "json",
            ]
            data = run_cli_json_command(func_cmd, env)

        if data is None:
            errors.append(f"Failed to fetch app settings for {app_name} in {rg}")
        else:
            app_settings[app_name] = data
            logger.info("Fetched %d app settings for %s", len(data), app_name)

    return app_settings, errors


def _fetch_dns_records(nodes_by_type, env):
    """Fetch DNS record sets for each discovered DNS zone.

    Args:
        nodes_by_type: Dict from _collect_nodes_by_type.
        env: Environment dict for subprocess.

    Returns:
        (dns_records_list, errors_list).
    """
    dns_zones = nodes_by_type.get("microsoft.network/dnszones", [])
    if not dns_zones:
        return [], []

    dns_records = []
    errors = []

    for zone in dns_zones:
        zone_name = zone["name"]
        rg = zone["resource_group"]
        cmd = [
            "az", "network", "dns", "record-set", "list",
            "--zone-name", zone_name,
            "--resource-group", rg,
            "--output", "json",
        ]
        data = run_cli_json_command(cmd, env)
        if data is None:
            errors.append(f"Failed to fetch DNS records for zone {zone_name} in {rg}")
        else:
            for record in data:
                record["_zone_name"] = zone_name
                record["_resource_group"] = rg
            dns_records.extend(data)
            logger.info("Fetched %d DNS record sets for zone %s", len(data), zone_name)

    return dns_records, errors


def enrich(user_id, azure_nodes, credentials):
    """Enrich Azure resources with detail data not available from Resource Graph.

    Only fetches data for resources that were discovered in Phase 1
    (present in ``azure_nodes``).

    Args:
        user_id: The Aurora user ID performing the enrichment.
        azure_nodes: List of Azure node dicts from Phase 1 discovery.
        credentials: Dict with optional keys:
            - tenant_id: Azure AD tenant ID
            - client_id: Service principal client ID
            - client_secret: Service principal client secret

    Returns:
        Dict with keys:
            - enrichment_data: Dict of enrichment categories.
            - errors: List of error message strings.
    """
    errors = []
    env = _build_env(credentials)

    logger.info("Starting Azure enrichment for user %s (%d nodes)", user_id, len(azure_nodes))

    nodes_by_type = _collect_nodes_by_type(azure_nodes)

    enrichment_data = {
        "nsg_rules": {},
        "app_settings": {},
        "dns_records": [],
    }

    # Fetch NSG rules
    nsg_rules, nsg_errors = _fetch_nsg_rules(nodes_by_type, env)
    enrichment_data["nsg_rules"] = nsg_rules
    errors.extend(nsg_errors)

    # Fetch app settings for web apps and function apps
    app_settings, app_errors = _fetch_app_settings(nodes_by_type, env)
    enrichment_data["app_settings"] = app_settings
    errors.extend(app_errors)

    # Fetch DNS records
    dns_records, dns_errors = _fetch_dns_records(nodes_by_type, env)
    enrichment_data["dns_records"] = dns_records
    errors.extend(dns_errors)

    logger.info(
        "Azure enrichment complete for user %s: %d NSGs, %d apps, %d DNS records, %d errors",
        user_id,
        len(nsg_rules),
        len(app_settings),
        len(dns_records),
        len(errors),
    )

    return {
        "enrichment_data": enrichment_data,
        "errors": errors,
    }
