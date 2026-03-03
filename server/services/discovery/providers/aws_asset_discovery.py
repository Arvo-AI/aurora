"""
AWS Asset Discovery - Phase 1 provider using AWS Resource Explorer 2.

Discovers all AWS resources via the Resource Explorer 2 search API
and maps them to normalized graph nodes using the resource_mapper.
"""

import json
import logging
import os
import subprocess

from services.discovery.resource_mapper import map_aws_resource

logger = logging.getLogger(__name__)



def _extract_name_from_arn(arn):
    """Extract a human-readable name from an ARN.

    ARN format: arn:aws:service:region:account:resource-type/resource-name
    or:         arn:aws:service:region:account:resource-name
    """
    if not arn:
        return "unknown"
    # Split on / first (most common), then on : for the remainder
    parts = arn.split("/")
    if len(parts) > 1:
        return parts[-1]
    # Fallback: split on : and take last segment
    colon_parts = arn.split(":")
    return colon_parts[-1] if colon_parts else "unknown"


def _extract_region_from_arn(arn):
    """Extract region from an ARN (4th colon-separated field)."""
    if not arn:
        return None
    parts = arn.split(":")
    if len(parts) >= 4:
        return parts[3] or None
    return None


def _build_env(credentials):
    """Build environment variables for AWS CLI subprocess calls."""
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = credentials["access_key_id"]
    env["AWS_SECRET_ACCESS_KEY"] = credentials["secret_access_key"]
    if credentials.get("session_token"):
        env["AWS_SESSION_TOKEN"] = credentials["session_token"]
    if credentials.get("region"):
        env["AWS_DEFAULT_REGION"] = credentials["region"]
    return env


def _find_aggregator_region(env):
    """Find the AWS region with an AGGREGATOR Resource Explorer index.

    Falls back to the current default region if no aggregator is found.
    """
    try:
        result = subprocess.run(
            ["aws", "resource-explorer-2", "list-indexes", "--output", "json"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        logger.info("list-indexes result: rc=%d, stdout=%s, stderr=%s",
                     result.returncode, result.stdout[:500], result.stderr[:200])
        if result.returncode == 0:
            data = json.loads(result.stdout)
            indexes = data.get("Indexes", [])
            # Prefer AGGREGATOR, fall back to any index
            for idx in indexes:
                if idx.get("Type") == "AGGREGATOR":
                    region = idx.get("Region")
                    if region:
                        logger.info("Found Resource Explorer AGGREGATOR index in region: %s", region)
                        return region
            # No aggregator — use the first available index
            if indexes:
                region = indexes[0].get("Region")
                if region:
                    logger.info("Found Resource Explorer LOCAL index in region: %s", region)
                    return region
    except Exception as e:
        logger.warning("Failed to detect aggregator region: %s", e)
    logger.info("No Resource Explorer index found, using default region")
    return None


def _run_resource_explorer_search(env, next_token=None):
    """Run a single AWS Resource Explorer 2 search call.

    Returns the parsed JSON response or raises an exception.
    """
    cmd = [
        "aws", "resource-explorer-2", "search",
        "--query-string", "",
        "--output", "json",
    ]
    if next_token:
        cmd.extend(["--next-token", next_token])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Check for common Resource Explorer setup issues
        if "ResourceNotFoundException" in stderr or "Index" in stderr:
            raise RuntimeError(
                "AWS Resource Explorer index is not set up. "
                "Please create an index by running: "
                "aws resource-explorer-2 create-index --type LOCAL "
                "in each region you want to discover, or create an "
                "AGGREGATOR index in your primary region."
            )
        raise RuntimeError(f"AWS CLI error (exit {result.returncode}): {stderr}")

    return json.loads(result.stdout)


def _properties_to_dict(properties_list):
    """Convert Resource Explorer Properties list to a flat dict.

    Properties come as [{"Name": "key", "Value": "val"}, ...].
    Values may be JSON strings that should be parsed.
    """
    props = {}
    if not properties_list:
        return props
    for prop in properties_list:
        name = prop.get("Name", "")
        value = prop.get("Value", "")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        props[name] = value
    return props


def _resource_to_node(resource):
    """Convert a single Resource Explorer resource to a graph node dict.

    Returns the node dict, or None if the resource type is not mapped.
    """
    arn = resource.get("Arn", "")
    aws_resource_type = resource.get("ResourceType", "")
    region = resource.get("Region") or _extract_region_from_arn(arn)

    resource_type, sub_type = map_aws_resource(aws_resource_type)
    if resource_type is None:
        return None

    name = _extract_name_from_arn(arn)
    properties = _properties_to_dict(resource.get("Properties"))

    # Try to extract a display name from properties, fall back to ARN-derived name
    display_name = properties.get("Name") or properties.get("Tags", {}).get("Name") if isinstance(properties.get("Tags"), dict) else None
    if not display_name:
        display_name = name

    # Try to extract endpoint from properties if available
    endpoint = (
        properties.get("Endpoint")
        or properties.get("DnsName")
        or properties.get("DomainName")
        or None
    )

    return {
        "name": name,
        "display_name": display_name,
        "resource_type": resource_type,
        "sub_type": sub_type,
        "provider": "aws",
        "region": region,
        "cloud_resource_id": arn,
        "endpoint": endpoint,
        "metadata": {
            "aws_resource_type": aws_resource_type,
            "properties": properties,
        },
    }


def discover(user_id, credentials, env=None):
    """Discover all AWS resources using Resource Explorer 2.

    Args:
        user_id: The Aurora user ID initiating the discovery.
        credentials: Dict with keys:
            - access_key_id (required): AWS access key ID
            - secret_access_key (required): AWS secret access key
            - region (optional): AWS region for Resource Explorer queries
            - role_arn (optional): IAM role ARN for cross-account access
        env: Unused (AWS builds its own env from credentials). Accepted for
             interface consistency with other providers.

    Returns:
        Dict with keys:
            - nodes: List of discovered resource node dicts
            - relationships: Empty list (Phase 1 - no relationship inference)
            - errors: List of error message strings
    """
    nodes = []
    errors = []

    # Validate required credentials
    if not credentials.get("access_key_id") or not credentials.get("secret_access_key"):
        return {
            "nodes": [],
            "relationships": [],
            "errors": ["AWS credentials missing: access_key_id and secret_access_key are required."],
        }

    env = _build_env(credentials)

    # Detect the aggregator region so we query the correct index
    aggregator_region = _find_aggregator_region(env)
    if aggregator_region:
        env["AWS_DEFAULT_REGION"] = aggregator_region

    logger.info("Starting AWS resource discovery for user %s", user_id)

    next_token = None
    page_count = 0

    try:
        while True:
            page_count += 1
            logger.info("Fetching AWS Resource Explorer page %d", page_count)

            response = _run_resource_explorer_search(env, next_token=next_token)
            resources = response.get("Resources", [])

            # Log resource types for debugging
            resource_types = set(r.get("ResourceType", "") for r in resources)
            logger.info("AWS Resource Explorer page %d: %d resources, types: %s", page_count, len(resources), resource_types)

            for resource in resources:
                try:
                    node = _resource_to_node(resource)
                    if node:
                        nodes.append(node)
                except Exception as e:
                    arn = resource.get("Arn", "unknown")
                    logger.warning("Failed to process AWS resource %s: %s", arn, e)
                    errors.append(f"Failed to process resource {arn}: {e}")

            next_token = response.get("NextToken")
            if not next_token:
                break

    except RuntimeError as e:
        logger.error("AWS Resource Explorer error: %s", e)
        errors.append(str(e))
    except subprocess.TimeoutExpired:
        logger.error("AWS CLI command timed out")
        errors.append("AWS CLI command timed out after 120 seconds.")
    except json.JSONDecodeError as e:
        logger.error("Failed to parse AWS CLI output: %s", e)
        errors.append(f"Failed to parse AWS CLI JSON output: {e}")
    except FileNotFoundError:
        logger.error("AWS CLI not found in PATH")
        errors.append(
            "AWS CLI is not installed or not found in PATH. "
            "Install it from https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
        )

    logger.info(
        "AWS discovery complete for user %s: %d nodes, %d errors",
        user_id, len(nodes), len(errors),
    )

    return {
        "nodes": nodes,
        "relationships": [],  # Phase 1: no relationship inference from Resource Explorer
        "errors": errors,
    }


def discover_all_accounts(user_id, account_envs):
    """Fan-out discovery across multiple AWS accounts and merge results.

    Args:
        user_id: The Aurora user ID.
        account_envs: List of dicts from setup_aws_environments_all_accounts(),
            each with keys: account_id, region, credentials, isolated_env.

    Returns:
        Merged dict with nodes, relationships, and errors across all accounts.
    """
    import concurrent.futures

    merged_nodes = []
    merged_errors = []

    def _discover_one(acct):
        creds = {
            "access_key_id": acct["credentials"]["accessKeyId"],
            "secret_access_key": acct["credentials"]["secretAccessKey"],
            "session_token": acct["credentials"]["sessionToken"],
            "region": acct["region"],
        }
        result = discover(user_id, creds)
        account_id = acct["account_id"]
        for node in result.get("nodes", []):
            node["aws_account_id"] = account_id
        result["errors"] = [f"[{account_id}] {err}" for err in result.get("errors", [])]
        return result

    if not account_envs:
        return {"nodes": [], "relationships": [], "errors": []}

    max_workers = min(len(account_envs), 10)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_discover_one, acct): acct["account_id"] for acct in account_envs}
        for future in concurrent.futures.as_completed(futures):
            account_id = futures[future]
            try:
                result = future.result()
                merged_nodes.extend(result.get("nodes", []))
                merged_errors.extend(result.get("errors", []))
            except Exception as e:
                logger.error("Discovery failed for account %s: %s", account_id, e)
                merged_errors.append(f"[{account_id}] Discovery failed: {e}")

    logger.info(
        "Multi-account AWS discovery complete for user %s: %d nodes across %d accounts, %d errors",
        user_id, len(merged_nodes), len(account_envs), len(merged_errors),
    )

    return {
        "nodes": merged_nodes,
        "relationships": [],
        "errors": merged_errors,
    }
