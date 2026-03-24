"""
Discovery Finding Tool

Agent tool for saving infrastructure discovery findings to the knowledge base.
Used by the prediscovery agent to persist interconnection mappings.
"""

import logging
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DISCOVERY_DOC_PREFIX = "discovery:"


class DiscoveryFindingArgs(BaseModel):
    """Arguments for saving a discovery finding."""

    title: str = Field(
        description="Short title for this finding, e.g. 'payment-service deployment chain'"
    )
    content: str = Field(
        description=(
            "Structured description of the interconnected services. Include service names, "
            "providers, connection types, repos, pipelines, monitoring, and how they relate."
        )
    )
    tags: str = Field(
        default="",
        description="Comma-separated tags for categorization, e.g. 'github,jenkins,k8s,prod'",
    )


def save_discovery_finding(
    title: str,
    content: str,
    tags: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """
    Save an infrastructure discovery finding to the knowledge base.

    Call this every time you discover how services are interconnected.
    Each finding should describe one logical chain or cluster of related services.

    Args:
        title: Short descriptive title for the finding
        content: Structured description of interconnected services
        tags: Comma-separated tags for categorization
        user_id: User identifier (injected by context wrapper)
        session_id: Session identifier (injected by context wrapper)

    Returns:
        Confirmation message or error
    """
    if not user_id:
        return "Error: User authentication required."

    if not title or not content:
        return "Error: Both title and content are required."

    try:
        from routes.knowledge_base.weaviate_client import insert_chunks
        from utils.auth.stateless_auth import get_org_id_for_user

        org_id = get_org_id_for_user(user_id)
        document_id = f"{DISCOVERY_DOC_PREFIX}{datetime.utcnow().strftime('%Y%m%d')}:{uuid.uuid4().hex[:8]}"

        chunks = [{
            "content": content,
            "heading_context": tags.strip() if tags else "infrastructure-discovery",
            "chunk_index": 0,
        }]

        inserted = insert_chunks(
            user_id=user_id,
            document_id=document_id,
            source_filename=f"[Auto-Discovery] {title}",
            chunks=chunks,
            org_id=org_id,
        )

        if inserted > 0:
            logger.info(f"[Discovery] Saved finding '{title}' (doc={document_id}) for org {org_id}")
            return f"Finding saved: '{title}' ({inserted} chunk(s) stored in knowledge base)"
        else:
            return "Warning: Finding was not stored. Weaviate may be unavailable."

    except Exception as e:
        logger.exception(f"[Discovery] Error saving finding: {e}")
        return f"Error saving finding: {str(e)}"


DISCOVERY_FINDING_DESCRIPTION = """Save an infrastructure discovery finding to the knowledge base.

Call this EVERY TIME you discover how services are interconnected. Do not accumulate findings - save each one immediately.

Each finding should describe one logical chain or cluster:
- Deployment chains: repo -> CI/CD pipeline -> deployment target
- Service dependencies: service A -> database B, cache C
- Monitoring mappings: which monitors/dashboards watch which services
- Network topology: load balancer -> backends, VPC groupings

Example:
  save_discovery_finding(
    title='payment-api deployment chain',
    content='GitHub repo org/payment-api deploys via Jenkins job payment-deploy to K8s cluster prod-east namespace payments. Image: ecr/payment-api. Depends on: RDS db-payments, ElastiCache redis-sessions. Monitored by Datadog monitors payment-api-latency and payment-api-errors.',
    tags='github,jenkins,k8s,aws,datadog'
  )"""
