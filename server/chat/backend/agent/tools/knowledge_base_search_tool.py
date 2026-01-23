"""
Knowledge Base Search Tool

Agent tool for searching the user's knowledge base during RCA investigations.
Uses hybrid search (semantic + keyword) with source attribution.
"""

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class KnowledgeBaseSearchArgs(BaseModel):
    """Arguments for knowledge base search."""

    query: str = Field(
        description="Search query for the knowledge base. Be specific - include service names, error types, or topics."
    )
    limit: int = Field(
        default=5,
        description="Maximum number of results to return (1-10).",
        ge=1,
        le=10,
    )


def knowledge_base_search(
    query: str,
    limit: int = 5,
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """
    Search the user's knowledge base for relevant documentation.

    Use this tool to find runbooks, architecture docs, and past incident reports.
    Always search at the START of any investigation to check for existing documentation.

    Args:
        query: Search query - be specific about the service, error, or topic
        limit: Maximum number of results (default 5)
        user_id: User identifier (injected by context wrapper)
        session_id: Session identifier (injected by context wrapper)

    Returns:
        Formatted search results with source citations, or message if no results
    """
    if not user_id:
        return "Error: User authentication required to search knowledge base."

    if not query or not query.strip():
        return "Error: Search query is required."

    # Clamp limit
    limit = max(1, min(10, limit))

    try:
        from routes.knowledge_base.weaviate_client import search_knowledge_base as search_kb

        results = search_kb(
            user_id=user_id,
            query=query.strip(),
            limit=limit,
            alpha=0.5,  # Balanced hybrid search
            min_score=0.0,  # Return all results, let agent decide relevance
        )

        if not results:
            return f"No relevant documents found in knowledge base for: '{query}'\n\nConsider:\n- The knowledge base may not have documentation for this topic\n- Try a different search query with alternative terms\n- Proceed with standard investigation approach"

        # Format results with source attribution
        output_parts = [f"Found {len(results)} relevant result(s) for: '{query}'\n"]

        for i, result in enumerate(results, 1):
            source = result.get("source_filename", "Unknown source")
            heading = result.get("heading_context", "")
            content = result.get("content", "")
            score = result.get("score", 0)

            # Build result block
            output_parts.append(f"--- Result {i} ---")
            output_parts.append(f"Source: {source}")
            if heading:
                output_parts.append(f"Section: {heading}")
            output_parts.append(f"Relevance: {score:.2f}")
            output_parts.append("")
            output_parts.append(content.strip())
            output_parts.append("")

        output_parts.append("---")
        output_parts.append(
            "Use this context to inform your investigation. "
            "Reference specific documents when providing recommendations."
        )

        logger.info(
            f"[KB Tool] Search '{query[:50]}...' returned {len(results)} results for user {user_id}"
        )

        return "\n".join(output_parts)

    except Exception as e:
        logger.exception(f"[KB Tool] Error searching knowledge base: {e}")
        return f"Error searching knowledge base: {str(e)}\n\nProceeding without knowledge base context."


# Tool description for the agent
KNOWLEDGE_BASE_SEARCH_DESCRIPTION = """Search your knowledge base for relevant documentation, runbooks, or context.

IMPORTANT: Use this tool at the START of any investigation to check for existing documentation.

When to use:
- Before investigating any service or system
- When you encounter an error or issue
- To find runbooks with troubleshooting steps
- To check for past incident reports or postmortems
- To understand service architecture and dependencies

Search tips:
- Include the service name: "spanner latency troubleshooting"
- Include the error type: "connection timeout redis"
- Search for patterns: "batch job conflict"
- Search for procedures: "escalation process database"

Returns relevant excerpts with source file attribution."""
