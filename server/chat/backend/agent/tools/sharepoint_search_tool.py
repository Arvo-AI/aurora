"""Agent tools for searching and interacting with SharePoint via Microsoft Graph API."""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from connectors.sharepoint_connector.search_service import SharePointSearchService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic arg schemas (visible to the LLM via StructuredTool)
# ---------------------------------------------------------------------------


class SharePointSearchArgs(BaseModel):
    query: str = Field(
        description="Search query string for SharePoint content (pages, documents, lists)"
    )
    site_id: Optional[str] = Field(
        default=None, description="SharePoint site ID to restrict search to a specific site"
    )
    max_results: int = Field(default=10, description="Maximum results to return")


class SharePointFetchPageArgs(BaseModel):
    site_id: str = Field(description="SharePoint site ID containing the page")
    page_id: str = Field(description="SharePoint page ID to fetch")
    max_length: int = Field(
        default=3000, description="Max markdown characters to return"
    )


class SharePointFetchDocumentArgs(BaseModel):
    drive_id: str = Field(description="SharePoint drive ID containing the document")
    item_id: str = Field(description="SharePoint item ID of the document to fetch")
    max_length: int = Field(
        default=3000, description="Max characters of extracted text to return"
    )


class SharePointCreatePageArgs(BaseModel):
    title: str = Field(description="Title for the new SharePoint page")
    content: str = Field(description="HTML or markdown content for the page body")
    site_id: Optional[str] = Field(
        default=None, description="SharePoint site ID to create the page in (uses default site if omitted)"
    )


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def sharepoint_search(
    query: str,
    site_id: Optional[str] = None,
    max_results: int = 10,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search SharePoint for pages, documents, and list items matching a query."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for SharePoint search")

    try:
        svc = SharePointSearchService(user_id)
        results = svc.search(
            query=query,
            site_id=site_id,
            max_results=max_results,
        )
    except Exception as exc:
        logger.exception(
            "SharePoint search failed for user %s: %s", user_id, exc
        )
        raise ValueError(
            "Failed to search SharePoint; check connection and permissions"
        ) from exc

    return json.dumps(
        {"status": "success", "count": len(results), "results": results},
        ensure_ascii=False,
    )


def sharepoint_fetch_page(
    site_id: str,
    page_id: str,
    max_length: int = 3000,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a SharePoint page by site and page ID and return its content as markdown."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for SharePoint page fetch")

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_page_markdown(site_id=site_id, page_id=page_id, max_length=max_length)
    except Exception as exc:
        logger.exception("SharePoint page fetch failed for user %s: %s", user_id, exc)
        raise ValueError(
            "Failed to fetch SharePoint page; check connection and permissions"
        ) from exc

    return json.dumps({"status": "success", **result}, ensure_ascii=False)


def sharepoint_fetch_document(
    drive_id: str,
    item_id: str,
    max_length: int = 3000,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a SharePoint document by drive and item ID and return extracted text."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for SharePoint document fetch")

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_document_text(drive_id=drive_id, item_id=item_id, max_length=max_length)
    except Exception as exc:
        logger.exception("SharePoint document fetch failed for user %s: %s", user_id, exc)
        raise ValueError(
            "Failed to fetch SharePoint document; check connection and permissions"
        ) from exc

    return json.dumps({"status": "success", **result}, ensure_ascii=False)


def sharepoint_create_page(
    title: str,
    content: str,
    site_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a new SharePoint page with the given title and content."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for SharePoint page creation")

    try:
        svc = SharePointSearchService(user_id)
        result = svc.create_page(title=title, markdown_content=content, site_id=site_id)
    except Exception as exc:
        logger.exception("SharePoint page creation failed for user %s: %s", user_id, exc)
        raise ValueError(
            "Failed to create SharePoint page; check connection and permissions"
        ) from exc

    return json.dumps({"status": "success", **result}, ensure_ascii=False)
