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


def _build_aws_env(credentials):
    """Build environment dict for AWS CLI calls."""
    env = os.environ.copy()
    aws_creds = credentials.get("aws", {})
    if aws_creds.get("access_key_id"):
        env["AWS_ACCESS_KEY_ID"] = aws_creds["access_key_id"]
    if aws_creds.get("secret_access_key"):
        env["AWS_SECRET_ACCESS_KEY"] = aws_creds["secret_access_key"]
    if aws_creds.get("region"):
        env["AWS_DEFAULT_REGION"] = aws_creds["region"]
    return env


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


def _fetch_cloud_run_env_vars(service_name, region):
    """Fetch environment variables for a GCP Cloud Run service.

    Returns a dict of env var key -> value, or empty dict on failure.
    """
    cmd = [
        "gcloud", "run", "services", "describe", service_name,
        f"--region={region}",
        "--format=json",
    ]
    data = run_cli_json_command(cmd)
    if data is None:
        return {}

    # Cloud Run: spec.template.spec.containers[0].env
    try:
        containers = data["spec"]["template"]["spec"]["containers"]
        env_list = containers[0].get("env", [])
        return {item["name"]: item.get("value", "") for item in env_list if "name" in item}
    except (KeyError, IndexError, TypeError):
        return {}


def _fetch_cloud_function_env_vars(function_name):
    """Fetch environment variables for a GCP Cloud Function.

    Returns a dict of env var key -> value, or empty dict on failure.
    """
    cmd = [
        "gcloud", "functions", "describe", function_name,
        "--format=json",
    ]
    data = run_cli_json_command(cmd)
    if data is None:
        return {}

    # Gen1: environmentVariables at top level
    # Gen2: serviceConfig.environmentVariables
    env_vars = data.get("environmentVariables", {})
    if not env_vars:
        service_config = data.get("serviceConfig", {})
        env_vars = service_config.get("environmentVariables", {})
    return env_vars or {}


def enrich(user_id, serverless_nodes, credentials_by_provider):
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

    Returns:
        Dict with keys:
            - env_vars: Dict mapping service name to its parsed dependencies.
            - errors: List of error message strings.
    """
    errors = []
    env_vars_result = {}

    logger.info(
        "Starting serverless enrichment for user %s (%d functions)",
        user_id, len(serverless_nodes),
    )

    aws_env = _build_aws_env(credentials_by_provider) if credentials_by_provider.get("aws") else None

    for node in serverless_nodes:
        provider = node.get("provider", "")
        name = node.get("name", "")
        sub_type = node.get("sub_type", "")
        region = node.get("region", "")

        if not name:
            continue

        raw_env_vars = {}

        try:
            if provider == "gcp" and sub_type == "cloud_run":
                raw_env_vars = _fetch_cloud_run_env_vars(name, region)

            elif provider == "gcp" and sub_type == "cloud_function":
                raw_env_vars = _fetch_cloud_function_env_vars(name)

            elif provider == "aws" and sub_type == "lambda":
                if aws_env is None:
                    errors.append(f"No AWS credentials for Lambda function {name}")
                    continue
                raw_env_vars = _fetch_lambda_env_vars(name, aws_env)

            elif provider == "azure":
                # Azure function app env vars are handled by azure_enrichment
                continue

            else:
                logger.debug(
                    "Skipping unsupported serverless type: provider=%s sub_type=%s name=%s",
                    provider, sub_type, name,
                )
                continue

        except Exception as e:
            msg = f"Failed to fetch env vars for {provider}/{name}: {e}"
            logger.warning(msg)
            errors.append(msg)
            continue

        if not raw_env_vars:
            logger.debug("No environment variables found for %s/%s", provider, name)
            continue

        # Parse dependencies from env vars (values are NOT stored)
        parsed_deps = _extract_dependencies_from_env(raw_env_vars)
        if parsed_deps:
            env_vars_result[name] = {
                "parsed_dependencies": parsed_deps,
            }
            logger.info(
                "Extracted %d dependency hints from %s/%s",
                len(parsed_deps), provider, name,
            )

    logger.info(
        "Serverless enrichment complete for user %s: %d services with dependencies, %d errors",
        user_id, len(env_vars_result), len(errors),
    )

    return {
        "env_vars": env_vars_result,
        "errors": errors,
    }
