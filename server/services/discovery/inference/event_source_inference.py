"""
Event Source / Queue Subscription Mapping - Phase 3 inference engine.

Infers DEPENDS_ON edges from event-driven architecture bindings:
  - Lambda event source mappings (SQS, Kinesis, DynamoDB streams)
  - SNS subscriptions (Protocol + Endpoint -> subscriber)
  - EventBridge rules (rule targets -> services)

The consumer DEPENDS_ON the queue/topic (event_trigger dependency type).
"""

import logging

from services.discovery.inference.node_lookup import (
    find_node_by_arn,
    find_node_by_endpoint,
    find_node_by_name,
)

logger = logging.getLogger(__name__)


def _infer_lambda_event_sources(event_sources, graph_nodes):
    """Infer edges from Lambda event source mappings.

    Each mapping connects an event source (SQS queue, Kinesis stream,
    DynamoDB stream) to a Lambda function that consumes from it.
    The Lambda function DEPENDS_ON the event source.
    """
    edges = []
    seen = set()

    for mapping in event_sources:
        function_arn = mapping.get("FunctionArn") or mapping.get("function_arn", "")
        source_arn = mapping.get("EventSourceArn") or mapping.get("event_source_arn", "")

        if not function_arn or not source_arn:
            continue

        # Find the Lambda function node
        consumer_node = find_node_by_arn(function_arn, graph_nodes)
        if not consumer_node:
            continue

        # Find the event source node
        source_node = find_node_by_arn(source_arn, graph_nodes)
        if not source_node:
            continue

        edge_key = (consumer_node, source_node)
        if edge_key not in seen:
            seen.add(edge_key)
            edges.append({
                "from_service": consumer_node,
                "to_service": source_node,
                "dependency_type": "event_trigger",
                "confidence": 0.9,
                "discovered_from": ["event_source"],
            })

    return edges


def _infer_sns_subscriptions(subscriptions, graph_nodes):
    """Infer edges from SNS subscriptions.

    Each subscription maps a topic to a subscriber (Lambda, SQS, HTTP endpoint, etc.).
    The subscriber DEPENDS_ON the SNS topic.
    """
    edges = []
    seen = set()

    for sub in subscriptions:
        topic_arn = sub.get("TopicArn") or sub.get("topic_arn", "")
        protocol = (sub.get("Protocol") or sub.get("protocol", "")).lower()
        endpoint = sub.get("Endpoint") or sub.get("endpoint", "")

        if not topic_arn or not endpoint:
            continue

        # Find the SNS topic node
        topic_node = find_node_by_arn(topic_arn, graph_nodes)
        if not topic_node:
            continue

        # Find the subscriber node based on protocol
        subscriber_node = None
        if protocol in ("lambda", "sqs"):
            # Endpoint is an ARN
            subscriber_node = find_node_by_arn(endpoint, graph_nodes)
        elif protocol in ("http", "https"):
            # Endpoint is a URL; try matching by endpoint field
            subscriber_node = find_node_by_endpoint(endpoint, graph_nodes)
        elif protocol == "email" or protocol == "email-json":
            # Email subscriptions don't map to infrastructure nodes
            continue

        if not subscriber_node:
            continue

        edge_key = (subscriber_node, topic_node)
        if edge_key not in seen:
            seen.add(edge_key)
            edges.append({
                "from_service": subscriber_node,
                "to_service": topic_node,
                "dependency_type": "event_trigger",
                "confidence": 0.9,
                "discovered_from": ["event_source"],
            })

    return edges


def _infer_eventbridge_rules(rules, graph_nodes):
    """Infer edges from EventBridge rules.

    Each rule can have targets (Lambda, SQS, Step Functions, etc.).
    The target DEPENDS_ON the EventBridge rule / event bus.
    """
    edges = []
    seen = set()

    for rule in rules:
        rule_name = rule.get("Name") or rule.get("name", "")
        rule_arn = rule.get("Arn") or rule.get("arn", "")
        targets = rule.get("Targets") or rule.get("targets", [])

        rule_node = find_node_by_arn(rule_arn, graph_nodes)
        if not rule_node:
            rule_node = find_node_by_name(rule_name, graph_nodes)
        if not rule_node:
            continue

        for target in targets:
            target_arn = target.get("Arn") or target.get("arn", "")
            if not target_arn:
                continue

            target_node = find_node_by_arn(target_arn, graph_nodes)
            if not target_node:
                continue

            edge_key = (target_node, rule_node)
            if edge_key not in seen:
                seen.add(edge_key)
                edges.append({
                    "from_service": target_node,
                    "to_service": rule_node,
                    "dependency_type": "event_trigger",
                    "confidence": 0.9,
                    "discovered_from": ["event_source"],
                })

    return edges


def infer(user_id, graph_nodes, enrichment_data):
    """Infer DEPENDS_ON edges from event source bindings.

    Processes three categories of event-driven architecture data:
      1. Lambda event source mappings (SQS, Kinesis, DynamoDB streams)
      2. SNS subscriptions
      3. EventBridge rules and their targets

    Args:
        user_id: The Aurora user ID.
        graph_nodes: List of service node dicts from Phase 1.
        enrichment_data: Dict from Phase 2 enrichment.

    Returns:
        List of dependency edge dicts with keys: from_service, to_service,
        dependency_type, confidence, discovered_from.
    """
    edges = []

    # Lambda event source mappings
    lambda_sources = enrichment_data.get("lambda_event_sources", [])
    if lambda_sources:
        lambda_edges = _infer_lambda_event_sources(lambda_sources, graph_nodes)
        edges.extend(lambda_edges)
        logger.info(
            "Event source inference for user %s: %d edges from %d Lambda event sources",
            user_id, len(lambda_edges), len(lambda_sources),
        )

    # SNS subscriptions
    sns_subs = enrichment_data.get("sns_subscriptions", [])
    if sns_subs:
        sns_edges = _infer_sns_subscriptions(sns_subs, graph_nodes)
        edges.extend(sns_edges)
        logger.info(
            "Event source inference for user %s: %d edges from %d SNS subscriptions",
            user_id, len(sns_edges), len(sns_subs),
        )

    # EventBridge rules
    eb_rules = enrichment_data.get("eventbridge_rules", [])
    if eb_rules:
        eb_edges = _infer_eventbridge_rules(eb_rules, graph_nodes)
        edges.extend(eb_edges)
        logger.info(
            "Event source inference for user %s: %d edges from %d EventBridge rules",
            user_id, len(eb_edges), len(eb_rules),
        )

    logger.info(
        "Event source inference complete for user %s: %d total edges",
        user_id, len(edges),
    )
    return edges
