"""
Graph Writer - Batch writes discovered nodes and edges to Memgraph.
Used by the discovery orchestrator after each phase completes.
"""

import logging

from services.graph.memgraph_client import get_memgraph_client

logger = logging.getLogger(__name__)


def write_services(user_id, services):
    """Batch upsert service nodes into Memgraph.

    Args:
        user_id: The user who owns these resources.
        services: List of dicts with keys: name, resource_type, provider, and optional props.

    Returns:
        Number of services successfully upserted.
    """
    if not services:
        return 0
    client = get_memgraph_client()
    count = client.batch_upsert_services(user_id, services)
    logger.info(f"Graph Writer: upserted {count}/{len(services)} services for user {user_id}")
    return count


def write_dependencies(user_id, dependencies):
    """Batch upsert dependency edges into Memgraph.

    Args:
        user_id: The user who owns these resources.
        dependencies: List of dicts with keys: from_service, to_service, dependency_type,
                      confidence, discovered_from.

    Returns:
        Number of dependencies successfully upserted.
    """
    if not dependencies:
        return 0
    client = get_memgraph_client()
    count = client.batch_upsert_dependencies(user_id, dependencies)
    logger.info(f"Graph Writer: upserted {count}/{len(dependencies)} dependencies for user {user_id}")
    return count
