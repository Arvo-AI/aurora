"""
AWS Detail Enrichment - Phase 2 discovery enrichment.

Fetches supplementary data that AWS Resource Explorer 2 does not provide,
such as security group rules, Lambda event source mappings, load balancer
target health, Route 53 hosted zones, SNS subscriptions, EventBridge rules,
and CloudMap service discovery details.

The enrichment data is consumed by Phase 3 inference to build dependency
edges between discovered nodes.
"""

import logging
import os

from services.discovery.enrichment.cli_utils import run_cli_json_command

logger = logging.getLogger(__name__)


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


def _fetch_security_groups(env):
    """Fetch all EC2 security groups with their inbound/outbound rules."""
    cmd = ["aws", "ec2", "describe-security-groups", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch security groups"
    return data.get("SecurityGroups", []), None


def _fetch_lambda_event_sources(env):
    """Fetch all Lambda event source mappings (SQS, Kinesis, DynamoDB, etc.)."""
    cmd = ["aws", "lambda", "list-event-source-mappings", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch Lambda event source mappings"
    return data.get("EventSourceMappings", []), None


def _fetch_lb_target_groups(env):
    """Fetch ELBv2 target groups and their target health status.

    For each target group, a secondary call fetches target health so
    Phase 3 can map load balancers to their backend services.
    """
    cmd = ["aws", "elbv2", "describe-target-groups", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch ELBv2 target groups"

    target_groups = data.get("TargetGroups", [])
    enriched = []

    for tg in target_groups:
        tg_arn = tg.get("TargetGroupArn", "")
        health_cmd = [
            "aws", "elbv2", "describe-target-health",
            "--target-group-arn", tg_arn,
            "--output", "json",
        ]
        health_data = run_cli_json_command(health_cmd, env)
        targets = []
        if health_data is not None:
            targets = health_data.get("TargetHealthDescriptions", [])

        enriched.append({
            "target_group_arn": tg_arn,
            "target_group_name": tg.get("TargetGroupName", ""),
            "load_balancer_arns": tg.get("LoadBalancerArns", []),
            "target_type": tg.get("TargetType", ""),
            "protocol": tg.get("Protocol"),
            "port": tg.get("Port"),
            "vpc_id": tg.get("VpcId"),
            "targets": targets,
        })

    return enriched, None


def _fetch_dns_records(env):
    """Fetch Route 53 hosted zones."""
    cmd = ["aws", "route53", "list-hosted-zones", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch Route 53 hosted zones"
    return data.get("HostedZones", []), None


def _fetch_sns_subscriptions(env):
    """Fetch all SNS subscriptions."""
    cmd = ["aws", "sns", "list-subscriptions", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch SNS subscriptions"
    return data.get("Subscriptions", []), None


def _fetch_eventbridge_rules(env):
    """Fetch all EventBridge rules."""
    cmd = ["aws", "events", "list-rules", "--output", "json"]
    data = run_cli_json_command(cmd, env)
    if data is None:
        return None, "Failed to fetch EventBridge rules"
    return data.get("Rules", []), None


def _fetch_cloudmap_services(env):
    """Fetch Cloud Map namespaces, services, and their registered instances."""
    # Step 1: List namespaces
    ns_cmd = ["aws", "servicediscovery", "list-namespaces", "--output", "json"]
    ns_data = run_cli_json_command(ns_cmd, env)
    if ns_data is None:
        return None, "Failed to fetch Cloud Map namespaces"

    namespaces = ns_data.get("Namespaces", [])

    # Step 2: List services
    svc_cmd = ["aws", "servicediscovery", "list-services", "--output", "json"]
    svc_data = run_cli_json_command(svc_cmd, env)
    services = svc_data.get("Services", []) if svc_data else []

    # Step 3: For each service, list its instances
    enriched_services = []
    for svc in services:
        service_id = svc.get("Id", "")
        instances_cmd = [
            "aws", "servicediscovery", "list-instances",
            "--service-id", service_id,
            "--output", "json",
        ]
        instances_data = run_cli_json_command(instances_cmd, env)
        instances = []
        if instances_data is not None:
            instances = instances_data.get("Instances", [])

        enriched_services.append({
            "service_id": service_id,
            "service_name": svc.get("Name", ""),
            "namespace_id": svc.get("NamespaceId", ""),
            "instances": instances,
        })

    return {
        "namespaces": namespaces,
        "services": enriched_services,
    }, None


def enrich(user_id, aws_nodes, credentials):
    """Enrich AWS resources with detail data not available from Resource Explorer 2.

    Args:
        user_id: The Aurora user ID performing the enrichment.
        aws_nodes: List of AWS node dicts from Phase 1 discovery.
        credentials: Dict with keys:
            - access_key_id (required): AWS access key ID
            - secret_access_key (required): AWS secret access key
            - region (optional): AWS default region

    Returns:
        Dict with keys:
            - enrichment_data: Dict of enrichment categories mapped by resource name.
            - errors: List of error message strings.
    """
    errors = []

    if not credentials.get("access_key_id") or not credentials.get("secret_access_key"):
        return {
            "enrichment_data": {},
            "errors": ["AWS credentials missing: access_key_id and secret_access_key are required."],
        }

    env = _build_env(credentials)

    logger.info("Starting AWS enrichment for user %s (%d nodes)", user_id, len(aws_nodes))

    enrichment_data = {
        "security_groups": [],
        "lambda_event_sources": [],
        "lb_target_groups": [],
        "dns_records": [],
        "sns_subscriptions": [],
        "eventbridge_rules": [],
        "cloudmap_services": [],
    }

    # Fetchers that return (list_data, error_or_None)
    list_fetchers = [
        ("security_groups", "security groups", _fetch_security_groups),
        ("lambda_event_sources", "Lambda event source mappings", _fetch_lambda_event_sources),
        ("lb_target_groups", "target groups with health data", _fetch_lb_target_groups),
        ("dns_records", "Route 53 hosted zones", _fetch_dns_records),
        ("sns_subscriptions", "SNS subscriptions", _fetch_sns_subscriptions),
        ("eventbridge_rules", "EventBridge rules", _fetch_eventbridge_rules),
    ]

    for key, label, fetcher in list_fetchers:
        data, err = fetcher(env)
        if err:
            errors.append(err)
        elif data is not None:
            enrichment_data[key] = data
            logger.info("Fetched %d %s", len(data), label)

    # Cloud Map returns a dict, not a list -- handle separately
    cm_data, cm_err = _fetch_cloudmap_services(env)
    if cm_err:
        errors.append(cm_err)
    elif cm_data is not None:
        enrichment_data["cloudmap_services"] = cm_data
        logger.info(
            "Fetched %d Cloud Map namespaces, %d services",
            len(cm_data.get("namespaces", [])),
            len(cm_data.get("services", [])),
        )

    logger.info(
        "AWS enrichment complete for user %s: %d errors",
        user_id, len(errors),
    )

    return {
        "enrichment_data": enrichment_data,
        "errors": errors,
    }
