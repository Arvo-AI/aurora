"""
Serverless Environment Variable Extraction - Phase 2 discovery enrichment.

For each serverless function discovered in Phase 1, fetches its configuration
to extract environment variable *keys* and parse them for dependency hints
(hostnames, ports, inferred dependency types).

SECURITY: This module never stores environment variable VALUES. It only
parses them at runtime to extract hostnames and inferred dependency types,
then discards the raw values immediately.
"""

import logging
import os
import re
from urllib.parse import urlparse

from services.discovery.enrichment.cli_utils import run_cli_json_command

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable key patterns -> dependency type
# ---------------------------------------------------------------------------

ENV_KEY_PATTERNS = [
    # Database
    (re.compile(r"(DATABASE_URL|DB_URL|DB_HOST|DB_HOSTNAME|POSTGRES_HOST|POSTGRES_URL|"
                r"PG_HOST|PGHOST|MYSQL_HOST|MYSQL_URL|MONGO_URL|MONGODB_URI|"
                r"MONGO_HOST|SQLALCHEMY_DATABASE_URI|JDBC_URL|DSN)", re.IGNORECASE),
     "database"),

    # Cache / Redis
    (re.compile(r"(REDIS_URL|REDIS_HOST|REDIS_ENDPOINT|CACHE_URL|CACHE_HOST|"
                r"MEMCACHED_HOST|MEMCACHE_SERVERS)", re.IGNORECASE),
     "cache"),

    # Message queues
    (re.compile(r"(KAFKA_BOOTSTRAP_SERVERS|KAFKA_BROKERS|KAFKA_URL|"
                r"RABBITMQ_URL|RABBITMQ_HOST|AMQP_URL|"
                r"SQS_QUEUE_URL|SQS_ENDPOINT|"
                r"PUBSUB_TOPIC|PUBSUB_SUBSCRIPTION|"
                r"NATS_URL|NATS_HOST)", re.IGNORECASE),
     "queue"),

    # Storage
    (re.compile(r"(S3_BUCKET|S3_ENDPOINT|AWS_S3_BUCKET|"
                r"GCS_BUCKET|GOOGLE_CLOUD_STORAGE_BUCKET|STORAGE_BUCKET|"
                r"AZURE_STORAGE_ACCOUNT|BLOB_STORAGE_URL|"
                r"MINIO_ENDPOINT)", re.IGNORECASE),
     "storage"),

    # API / service endpoints
    (re.compile(r"(API_URL|API_HOST|API_ENDPOINT|API_BASE_URL|"
                r"SERVICE_URL|SERVICE_HOST|SERVICE_ENDPOINT|"
                r"BACKEND_URL|BACKEND_HOST|"
                r"AUTH_URL|AUTH_HOST|AUTH_ENDPOINT|"
                r"GRAPHQL_URL|GRAPHQL_ENDPOINT)", re.IGNORECASE),
     "api"),

    # Search
    (re.compile(r"(ELASTICSEARCH_URL|ELASTICSEARCH_HOST|ES_HOST|ES_URL|"
                r"OPENSEARCH_URL|OPENSEARCH_HOST|SOLR_URL|SOLR_HOST|"
                r"ALGOLIA_APP_ID|MEILISEARCH_HOST)", re.IGNORECASE),
     "search"),

    # Email
    (re.compile(r"(SMTP_HOST|SMTP_SERVER|MAIL_HOST|MAIL_SERVER|"
                r"SENDGRID_API_KEY|MAILGUN_DOMAIN)", re.IGNORECASE),
     "email"),
]

# Ports that hint at a dependency type
PORT_TYPE_HINTS = {
    5432: "database",    # PostgreSQL
    3306: "database",    # MySQL
    27017: "database",   # MongoDB
    6379: "cache",       # Redis
    11211: "cache",      # Memcached
    9092: "queue",       # Kafka
    5672: "queue",       # RabbitMQ
    4222: "queue",       # NATS
    9200: "search",      # Elasticsearch
    9243: "search",      # Elastic Cloud
    7700: "search",      # MeiliSearch
    443: "api",          # HTTPS
    80: "api",           # HTTP
    8080: "api",         # Alt HTTP
    8443: "api",         # Alt HTTPS
}


def _classify_env_key(key):
    """Return the dependency type for an environment variable key, or None."""
    for pattern, dep_type in ENV_KEY_PATTERNS:
        if pattern.search(key):
            return dep_type
    return None


def _parse_url_value(value):
    """Parse a URL-style value and extract hostname, port, and type hint.

    Handles formats like:
        postgresql://user:pass@host:5432/dbname
        redis://host:6379/0
        https://api.example.com/v1
        host:port

    Returns:
        Dict with hostname, port, and inferred type, or None if unparseable.
    """
    if not value or not isinstance(value, str):
        return None

    # Skip values that are obviously not URLs or hostnames
    value = value.strip()
    if not value or value.startswith("/") or len(value) < 3:
        return None

    # Try standard URL parsing first
    try:
        parsed = urlparse(value)
        if parsed.hostname:
            port = parsed.port
            dep_type = None

            # Infer type from scheme
            scheme = (parsed.scheme or "").lower()
            scheme_types = {
                "postgresql": "database", "postgres": "database",
                "mysql": "database", "mongodb": "database",
                "mongodb+srv": "database",
                "redis": "cache", "rediss": "cache",
                "amqp": "queue", "amqps": "queue",
                "kafka": "queue",
                "https": "api", "http": "api",
            }
            dep_type = scheme_types.get(scheme)

            # Refine with port hint if available
            if port and port in PORT_TYPE_HINTS:
                dep_type = PORT_TYPE_HINTS[port]

            return {
                "hostname": parsed.hostname,
                "port": port,
                "type": dep_type,
            }
    except Exception:
        pass

    # Try host:port format
    host_port_match = re.match(r"^([a-zA-Z0-9._-]+):(\d+)$", value)
    if host_port_match:
        hostname = host_port_match.group(1)
        port = int(host_port_match.group(2))
        dep_type = PORT_TYPE_HINTS.get(port)
        return {
            "hostname": hostname,
            "port": port,
            "type": dep_type,
        }

    # Try bare hostname (must have at least one dot)
    if re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$", value) and "." in value:
        return {
            "hostname": value,
            "port": None,
            "type": None,
        }

    return None


def _extract_dependencies_from_env(env_vars):
    """Extract dependency hints from a dict of environment variables.

    Args:
        env_vars: Dict mapping env var names to values.

    Returns:
        List of parsed dependency dicts with keys:
            hostname, port, type, env_key.
    """
    dependencies = []
    seen_hostnames = set()

    for key, value in env_vars.items():
        # Classify by key pattern
        key_type = _classify_env_key(key)
        if key_type is None:
            continue

        # Try to parse the value for hostname/port
        parsed = _parse_url_value(value)
        if parsed and parsed.get("hostname"):
            hostname = parsed["hostname"]

            # Skip localhost and container-internal references
            if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
                continue

            # Deduplicate by hostname
            if hostname in seen_hostnames:
                continue
            seen_hostnames.add(hostname)

            dep_type = parsed.get("type") or key_type
            dependencies.append({
                "hostname": hostname,
                "port": parsed.get("port"),
                "type": dep_type,
                "env_key": key,
            })
        elif key_type:
            # Key matched a pattern but value was not a parseable URL.
            # Still record the dependency hint with the key type.
            dependencies.append({
                "hostname": None,
                "port": None,
                "type": key_type,
                "env_key": key,
            })

    return dependencies


# ---------------------------------------------------------------------------
# Provider-specific fetchers
# ---------------------------------------------------------------------------


def _extract_account_id_from_arn(arn):
    """Extract the AWS account ID from a Lambda ARN.

    ARN format: arn:aws:lambda:<region>:<account-id>:function:<name>
    Returns the account ID string, or None if the ARN is unparseable.
    """
    if not arn or not arn.startswith("arn:aws"):
        return None
    parts = arn.split(":")
    return parts[4] if len(parts) >= 5 and parts[4] else None


def _build_env_for_account(acct):
    """Return a minimal subprocess env dict for a single AWS account entry.

    Prefers the ready-made ``isolated_env`` when present; otherwise constructs
    one from the raw credential keys.  Never copies ``os.environ`` wholesale.
    """
    if acct.get("isolated_env"):
        return dict(acct["isolated_env"])
    raw = acct.get("credentials", {})
    env = {"PATH": os.environ.get("PATH", "")}
    if raw.get("accessKeyId"):
        env["AWS_ACCESS_KEY_ID"] = raw["accessKeyId"]
    if raw.get("secretAccessKey"):
        env["AWS_SECRET_ACCESS_KEY"] = raw["secretAccessKey"]
    if raw.get("sessionToken"):
        env["AWS_SESSION_TOKEN"] = raw["sessionToken"]
    if acct.get("region"):
        env["AWS_DEFAULT_REGION"] = acct["region"]
    return env


def _build_single_account_env(aws_creds):
    """Return a minimal subprocess env dict for single-account AWS credentials."""
    env = {"PATH": os.environ.get("PATH", "")}
    if aws_creds.get("access_key_id"):
        env["AWS_ACCESS_KEY_ID"] = aws_creds["access_key_id"]
    if aws_creds.get("secret_access_key"):
        env["AWS_SECRET_ACCESS_KEY"] = aws_creds["secret_access_key"]
    if aws_creds.get("session_token"):
        env["AWS_SESSION_TOKEN"] = aws_creds["session_token"]
    if aws_creds.get("region"):
        env["AWS_DEFAULT_REGION"] = aws_creds["region"]
    return env


def _build_aws_account_envs(credentials):
    """Build a dict mapping account_id -> subprocess env for multi-account AWS.

    For single-account credentials, returns {None: env} so callers can use
    the same lookup pattern regardless of the auth mode.

    Uses ``isolated_env`` from each account entry when available (already
    built by setup_aws_environments_all_accounts), otherwise constructs one
    from the credential keys.
    """
    aws_creds = credentials.get("aws", {})

    if aws_creds.get("_multi_account"):
        account_envs = aws_creds.get("_account_envs") or []
        result = {}
        for acct in account_envs:
            acct_id = acct.get("account_id")
            if acct_id:
                result[acct_id] = _build_env_for_account(acct)
        return result

    # Single-account: key by None so callers always do env_map.get(acct_id, fallback).
    return {None: _build_single_account_env(aws_creds)}


def _fetch_lambda_env_vars(function_name, aws_env):
    """Fetch environment variables for an AWS Lambda function.

    Returns a dict of env var key -> value, or empty dict on failure.
    """
    cmd = [
        "aws", "lambda", "get-function-configuration",
        "--function-name", function_name,
        "--output", "json",
    ]
    data = run_cli_json_command(cmd, env=aws_env)
    if data is None:
        return {}
    return data.get("Environment", {}).get("Variables", {})


def _fetch_cloud_run_env_vars(service_name, region, project, gcp_env=None):
    """Fetch environment variables for a GCP Cloud Run service.

    Returns a dict of env var key -> value, or empty dict on failure.
    """
    cmd = [
        "gcloud", "run", "services", "describe", service_name,
        f"--region={region}",
        f"--project={project}",
        "--format=json",
    ]
    data = run_cli_json_command(cmd, env=gcp_env)
    if data is None:
        return {}

    # Cloud Run: spec.template.spec.containers[0].env
    try:
        containers = data["spec"]["template"]["spec"]["containers"]
        env_list = containers[0].get("env", [])
        return {item["name"]: item.get("value", "") for item in env_list if "name" in item}
    except (KeyError, IndexError, TypeError):
        return {}


def _fetch_cloud_function_env_vars(function_name, project, gcp_env=None):
    """Fetch environment variables for a GCP Cloud Function.

    Returns a dict of env var key -> value, or empty dict on failure.
    """
    cmd = [
        "gcloud", "functions", "describe", function_name,
        f"--project={project}",
        "--format=json",
    ]
    data = run_cli_json_command(cmd, env=gcp_env)
    if data is None:
        return {}

    # Gen1: environmentVariables at top level
    # Gen2: serviceConfig.environmentVariables
    env_vars = data.get("environmentVariables", {})
    if not env_vars:
        service_config = data.get("serviceConfig", {})
        env_vars = service_config.get("environmentVariables", {})
    return env_vars or {}


def _fetch_raw_env_vars(node, aws_account_envs, _any_aws_env, gcp_env, gcp_default_project):
    """Fetch raw environment variable dict for a single serverless node.

    Returns the raw env-vars dict, or raises on failure.
    Returns None when the node should be skipped (unsupported type or no creds).
    """
    provider = node.get("provider", "")
    name = node.get("name", "")
    sub_type = node.get("sub_type", "")
    region = node.get("region", "")
    project = node.get("metadata", {}).get("project_id") or gcp_default_project

    if provider == "gcp" and sub_type == "cloud_run":
        return _fetch_cloud_run_env_vars(name, region, project, gcp_env)

    if provider == "gcp" and sub_type == "cloud_function":
        return _fetch_cloud_function_env_vars(name, project, gcp_env)

    if provider == "aws" and sub_type == "lambda":
        if _any_aws_env is None:
            raise ValueError(f"No AWS credentials for Lambda function {name}")
        node_arn = node.get("cloud_resource_id", "")
        acct_id = _extract_account_id_from_arn(node_arn)
        aws_env = aws_account_envs.get(acct_id) or _any_aws_env
        if acct_id and acct_id not in aws_account_envs:
            logger.warning(
                "AWS Lambda %s: account %s not in credentials map — falling back to a different account's credentials",
                name, acct_id,
            )
        return _fetch_lambda_env_vars(name, aws_env)

    if provider == "azure":
        return None  # Handled by azure_enrichment

    logger.debug(
        "Skipping unsupported serverless type: provider=%s sub_type=%s name=%s",
        provider, sub_type, name,
    )
    return None


def _process_serverless_node(node, aws_account_envs, _any_aws_env, gcp_env, gcp_default_project):
    """Fetch and parse env-var dependencies for a single serverless node.

    Returns:
        (name, parsed_deps, error_msg) where:
            - name is None when the node should be skipped entirely.
            - parsed_deps is a list (possibly empty) of dependency dicts.
            - error_msg is a non-None string when an error should be recorded.
    """
    name = node.get("name", "")
    provider = node.get("provider", "")
    if not name:
        return None, [], None

    try:
        raw_env_vars = _fetch_raw_env_vars(
            node, aws_account_envs, _any_aws_env, gcp_env, gcp_default_project
        )
    except ValueError as e:
        return name, [], str(e)
    except Exception as e:
        msg = f"Failed to fetch env vars for {provider}/{name}: {e}"
        logger.warning(msg)
        return name, [], msg

    if raw_env_vars is None:
        return None, [], None

    if not raw_env_vars:
        logger.debug("No environment variables found for %s/%s", provider, name)
        return name, [], None

    parsed_deps = _extract_dependencies_from_env(raw_env_vars)
    if parsed_deps:
        logger.info(
            "Extracted %d dependency hints from %s/%s",
            len(parsed_deps), provider, name,
        )
    return name, parsed_deps, None


def enrich(user_id, serverless_nodes, credentials_by_provider, provider_envs=None):
    """Extract environment variable dependency hints from serverless functions.

    For each serverless function discovered in Phase 1, fetches its
    configuration, extracts environment variable keys, and parses them
    for hostnames and dependency type hints. Environment variable VALUES
    are never persisted.

    Args:
        user_id: The Aurora user ID performing the enrichment.
        serverless_nodes: List of serverless node dicts from Phase 1. Each
            node must have ``provider``, ``name``, ``sub_type``, and
            optionally ``region`` fields.
        credentials_by_provider: Dict mapping provider name to credentials
            dict. Expected keys: ``aws``, ``gcp``, ``azure``.
        provider_envs: Optional dict mapping provider name to the isolated
            subprocess env dict built during Phase 1 (used for GCP gcloud
            auth). When omitted, GCP enrichment runs without explicit auth.

    Returns:
        Dict with keys:
            - env_vars: Dict mapping service name to its parsed dependencies.
            - errors: List of error message strings.
    """
    if provider_envs is None:
        provider_envs = {}

    logger.info(
        "Starting serverless enrichment for user %s (%d functions)",
        user_id, len(serverless_nodes),
    )

    # Build per-account AWS env map. For single-account this is {None: env};
    # for multi-account it is {account_id: env, ...} so each Lambda gets
    # credentials scoped to the account that owns it.
    aws_account_envs = _build_aws_account_envs(credentials_by_provider) if credentials_by_provider.get("aws") else {}
    # Sentinel: any non-None env from the map (used for "has any AWS creds" check)
    _any_aws_env = next(iter(aws_account_envs.values()), None) if aws_account_envs else None

    # GCP: extract the isolated subprocess env and a default project ID for
    # gcloud commands that require --project (Cloud Run describe, Functions describe).
    gcp_env = provider_envs.get("gcp")
    gcp_creds = credentials_by_provider.get("gcp", {})
    gcp_project_ids = gcp_creds.get("project_ids") or []
    gcp_default_project = gcp_project_ids[0] if gcp_project_ids else gcp_creds.get("project_id", "")

    errors = []
    env_vars_result = {}
    for node in serverless_nodes:
        name, parsed_deps, error_msg = _process_serverless_node(
            node, aws_account_envs, _any_aws_env, gcp_env, gcp_default_project
        )
        if error_msg:
            errors.append(error_msg)
        if name and parsed_deps:
            env_vars_result[name] = {"parsed_dependencies": parsed_deps}

    logger.info(
        "Serverless enrichment complete for user %s: %d services with dependencies, %d errors",
        user_id, len(env_vars_result), len(errors),
    )

    return {
        "env_vars": env_vars_result,
        "errors": errors,
    }
