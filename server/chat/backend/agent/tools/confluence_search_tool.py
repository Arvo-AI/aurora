"""Agent tools for searching Confluence via CQL."""

import json
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from connectors.confluence_connector.search_service import ConfluenceSearchService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic arg schemas (visible to the LLM via StructuredTool)
# ---------------------------------------------------------------------------


class ConfluenceSearchSimilarArgs(BaseModel):
    keywords: List[str] = Field(
        description="Keywords describing the incident (e.g. ['connection timeout', 'redis'])"
    )
    service_name: Optional[str] = Field(
        default=None, description="Affected service name"
    )
    error_message: Optional[str] = Field(
        default=None, description="Raw error message snippet"
    )
    spaces: Optional[List[str]] = Field(
        default=None, description="Confluence space keys to restrict search"
    )
    max_results: int = Field(default=10, description="Maximum results to return")


class ConfluenceSearchRunbookArgs(BaseModel):
    service_name: str = Field(description="Service to find runbooks for")
    operation: Optional[str] = Field(
        default=None, description="Specific operation (e.g. 'restart', 'failover')"
    )
    spaces: Optional[List[str]] = Field(
        default=None, description="Confluence space keys to restrict search"
    )


class ConfluenceFetchPageArgs(BaseModel):
    page_id: str = Field(description="Confluence page ID to fetch")
    max_length: int = Field(
        default=3000, description="Max markdown characters to return"
    )


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def confluence_search_similar(
    keywords: List[str],
    service_name: Optional[str] = None,
    error_message: Optional[str] = None,
    spaces: Optional[List[str]] = None,
    max_results: int = 10,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search Confluence for pages similar to the current incident."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Confluence search")

    try:
        svc = ConfluenceSearchService(user_id)
        results = svc.search_similar_incidents(
            keywords=keywords,
            service_name=service_name,
            error_message=error_message,
            spaces=spaces,
            max_results=max_results,
        )
    except Exception as exc:
        logger.exception(
            "Confluence similar-incidents search failed for user %s: %s", user_id, exc
        )
        raise ValueError(
            "Failed to search Confluence; check connection and permissions"
        ) from exc

    return json.dumps(
        {"status": "success", "count": len(results), "results": results},
        ensure_ascii=False,
    )


def confluence_search_runbooks(
    service_name: str,
    operation: Optional[str] = None,
    spaces: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search Confluence for runbooks related to a service."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Confluence search")

    try:
        svc = ConfluenceSearchService(user_id)
        results = svc.search_runbooks(
            service_name=service_name,
            operation=operation,
            spaces=spaces,
        )
    except Exception as exc:
        logger.exception(
            "Confluence runbook search failed for user %s: %s", user_id, exc
        )
        raise ValueError(
            "Failed to search Confluence; check connection and permissions"
        ) from exc

    return json.dumps(
        {"status": "success", "count": len(results), "results": results},
        ensure_ascii=False,
    )


def confluence_fetch_page(
    page_id: str,
    max_length: int = 3000,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a Confluence page by ID and return its markdown content."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Confluence page fetch")

    try:
        svc = ConfluenceSearchService(user_id)
        result = svc.fetch_page_markdown(page_id, max_length=max_length)
    except Exception as exc:
        logger.exception("Confluence page fetch failed for user %s: %s", user_id, exc)
        raise ValueError(
            "Failed to fetch Confluence page; check connection and permissions"
        ) from exc

    return json.dumps({"status": "success", **result}, ensure_ascii=False)
