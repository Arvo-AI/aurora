"""
AWS Detail Enrichment - Phase 2 discovery enrichment.

Fetches supplementary data that AWS Resource Explorer 2 does not provide,
such as security group rules, Lambda event source mappings, load balancer
target health, Route 53 hosted zones, SNS subscriptions, EventBridge rules,
and CloudMap service discovery details.

The enrichment data is consumed by Phase 3 inference to build dependency
edges between discovered nodes.
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _build_env(credentials):
    """Build environment variables for AWS CLI subprocess calls."""
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = credentials["access_key_id"]
    env["AWS_SECRET_ACCESS_KEY"] = credentials["secret_access_key"]
    if credentials.get("region"):
        env["AWS_DEFAULT_REGION"] = credentials["region"]
    return env


def _run_aws_command(cmd, env, timeout=120):
    """Run an AWS CLI command and return parsed JSON output.

    Args:
        cmd: List of command arguments.
        env: Environment dict with AWS credentials.
        timeout: Command timeout in seconds.

    Returns:
        Parsed JSON output, or None on failure.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning(
                "AWS CLI command failed (exit %d): %s — cmd: %s",
                result.returncode,
                result.stderr.strip(),
                " ".join(cmd),
            )
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("AWS CLI command timed out after %ds: %s", timeout, " ".join(cmd))
        return None
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse AWS CLI JSON output: %s", e)
        return None
    except FileNotFoundError:
        logger.error("AWS CLI not found in PATH")
        return None


def _fetch_security_groups(env):
    """Fetch all EC2 security groups with their inbound/outbound rules."""
    cmd = ["aws", "ec2", "describe-security-groups", "--output", "json"]
    data = _run_aws_command(cmd, env)
    if data is None:
        return None, "Failed to fetch security groups"
    return data.get("SecurityGroups", []), None


def _fetch_lambda_event_sources(env):
    """Fetch all Lambda event source mappings (SQS, Kinesis, DynamoDB, etc.)."""
    cmd = ["aws", "lambda", "list-event-source-mappings", "--output", "json"]
    data = _run_aws_command(cmd, env)
    if data is None:
        return None, "Failed to fetch Lambda event source mappings"
    return data.get("EventSourceMappings", []), None


def _fetch_lb_target_groups(env):
    """Fetch ELBv2 target groups and their target health status.

    For each target group, a secondary call fetches target health so
    Phase 3 can map load balancers to their backend services.
    """
    cmd = ["aws", "elbv2", "describe-target-groups", "--output", "json"]
    data = _run_aws_command(cmd, env)
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
        health_data = _run_aws_command(health_cmd, env)
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
    data = _run_aws_command(cmd, env)
    if data is None:
        return None, "Failed to fetch Route 53 hosted zones"
    return data.get("HostedZones", []), None


def _fetch_sns_subscriptions(env):
    """Fetch all SNS subscriptions."""
    cmd = ["aws", "sns", "list-subscriptions", "--output", "json"]
    data = _run_aws_command(cmd, env)
    if data is None:
        return None, "Failed to fetch SNS subscriptions"
    return data.get("Subscriptions", []), None


def _fetch_eventbridge_rules(env):
    """Fetch all EventBridge rules."""
    cmd = ["aws", "events", "list-rules", "--output", "json"]
    data = _run_aws_command(cmd, env)
    if data is None:
        return None, "Failed to fetch EventBridge rules"
    return data.get("Rules", []), None


def _fetch_cloudmap_services(env):
    """Fetch Cloud Map namespaces, services, and their registered instances."""
    # Step 1: List namespaces
    ns_cmd = ["aws", "servicediscovery", "list-namespaces", "--output", "json"]
    ns_data = _run_aws_command(ns_cmd, env)
    if ns_data is None:
        return None, "Failed to fetch Cloud Map namespaces"

    namespaces = ns_data.get("Namespaces", [])

    # Step 2: List services
    svc_cmd = ["aws", "servicediscovery", "list-services", "--output", "json"]
    svc_data = _run_aws_command(svc_cmd, env)
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
        instances_data = _run_aws_command(instances_cmd, env)
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

    # Fetch security groups
    sg_data, sg_err = _fetch_security_groups(env)
    if sg_err:
        errors.append(sg_err)
    elif sg_data is not None:
        enrichment_data["security_groups"] = sg_data
        logger.info("Fetched %d security groups", len(sg_data))

    # Fetch Lambda event source mappings
    esm_data, esm_err = _fetch_lambda_event_sources(env)
    if esm_err:
        errors.append(esm_err)
    elif esm_data is not None:
        enrichment_data["lambda_event_sources"] = esm_data
        logger.info("Fetched %d Lambda event source mappings", len(esm_data))

    # Fetch ELBv2 target groups with health
    tg_data, tg_err = _fetch_lb_target_groups(env)
    if tg_err:
        errors.append(tg_err)
    elif tg_data is not None:
        enrichment_data["lb_target_groups"] = tg_data
        logger.info("Fetched %d target groups with health data", len(tg_data))

    # Fetch Route 53 hosted zones
    dns_data, dns_err = _fetch_dns_records(env)
    if dns_err:
        errors.append(dns_err)
    elif dns_data is not None:
        enrichment_data["dns_records"] = dns_data
        logger.info("Fetched %d Route 53 hosted zones", len(dns_data))

    # Fetch SNS subscriptions
    sns_data, sns_err = _fetch_sns_subscriptions(env)
    if sns_err:
        errors.append(sns_err)
    elif sns_data is not None:
        enrichment_data["sns_subscriptions"] = sns_data
        logger.info("Fetched %d SNS subscriptions", len(sns_data))

    # Fetch EventBridge rules
    eb_data, eb_err = _fetch_eventbridge_rules(env)
    if eb_err:
        errors.append(eb_err)
    elif eb_data is not None:
        enrichment_data["eventbridge_rules"] = eb_data
        logger.info("Fetched %d EventBridge rules", len(eb_data))

    # Fetch Cloud Map services
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
