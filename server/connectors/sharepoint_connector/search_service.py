"""High-level SharePoint search service with auth-refresh-retry."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from connectors.sharepoint_connector.auth import refresh_access_token
from connectors.sharepoint_connector.client import (
    SharePointClient,
    markdown_to_sharepoint_html,
)
from connectors.sharepoint_connector.content_parser import (
    extract_document_text,
    sharepoint_page_to_markdown,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)


class SharePointSearchService:
    """Searches SharePoint via Microsoft Graph with automatic token refresh on 401."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._creds = get_token_data(user_id, "sharepoint")
        if not self._creds:
            raise ValueError(f"No SharePoint credentials for user {user_id}")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        site_id: Optional[str] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search SharePoint for documents, pages, and sites.

        Args:
            query: The search query string.
            site_id: Optional site ID to scope the search.
            max_results: Maximum number of results to return.

        Returns:
            List of search hit dictionaries.
        """
        def _do_search(client: SharePointClient) -> List[Dict[str, Any]]:
            search_query = query
            if site_id:
                search_query = f"{query} site:{site_id}"

            raw = client.search(
                search_query,
                entity_types=["driveItem", "listItem", "site"],
                size=max_results,
            )
            return _extract_search_hits(raw)

        return self._retry_with_refresh(_do_search)

    def fetch_page_markdown(
        self,
        site_id: str,
        page_id: str,
        max_length: int = 3000,
    ) -> Dict[str, Any]:
        """Fetch a SharePoint page and return its content as markdown.

        Args:
            site_id: The SharePoint site ID.
            page_id: The page ID to fetch.
            max_length: Maximum length of the returned markdown (0 = unlimited).

        Returns:
            Dictionary with ``pageId``, ``title``, and ``markdown`` keys.
        """
        def _do_fetch(client: SharePointClient) -> Dict[str, Any]:
            page = client.get_page(site_id=site_id, page_id=page_id)
            canvas_layout = page.get("canvasLayout") or {}
            md = sharepoint_page_to_markdown(canvas_layout)
            if max_length and len(md) > max_length:
                md = md[:max_length] + "\n\n... [truncated]"
            return {
                "pageId": page.get("id") or page_id,
                "title": page.get("title"),
                "markdown": md,
            }

        return self._retry_with_refresh(_do_fetch)

    def fetch_document_text(
        self,
        drive_id: str,
        item_id: str,
        max_length: int = 3000,
    ) -> Dict[str, Any]:
        """Download a document and extract its text content.

        Args:
            drive_id: The drive (document library) ID.
            item_id: The drive item ID.
            max_length: Maximum length of the returned text (0 = unlimited).

        Returns:
            Dictionary with ``itemId``, ``name``, and ``text`` keys.
        """
        def _do_fetch(client: SharePointClient) -> Dict[str, Any]:
            metadata = client.get_drive_item(drive_id, item_id)
            filename = metadata.get("name", "")
            file_bytes = client.download_drive_item(drive_id, item_id)
            text = extract_document_text(file_bytes, filename)
            if max_length and len(text) > max_length:
                text = text[:max_length] + "\n\n... [truncated]"
            return {
                "itemId": metadata.get("id") or item_id,
                "name": filename,
                "text": text,
            }

        return self._retry_with_refresh(_do_fetch)

    def create_page(
        self,
        title: str,
        markdown_content: str,
        site_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create and publish a new SharePoint page from markdown content.

        Args:
            title: Page title.
            markdown_content: Page body as markdown (converted to HTML automatically).
            site_id: Target site ID (falls back to credentials' default site).

        Returns:
            The created page resource from the Graph API.
        """
        def _do_create(client: SharePointClient) -> Dict[str, Any]:
            stripped = markdown_content.strip()
            if stripped.startswith("<") and (">" in stripped):
                html_content = stripped
            else:
                html_content = markdown_to_sharepoint_html(markdown_content)
            sid = site_id or self._creds.get("site_id")
            if not sid:
                raise ValueError(
                    "site_id is required to create a SharePoint page"
                )
            page = client.create_page(title, html_content, site_id=sid)
            page_id = page.get("id")
            if page_id:
                try:
                    client.publish_page(site_id=sid, page_id=page_id)
                except Exception as exc:
                    logger.warning("Created page but failed to publish: %s", type(exc).__name__)
            return page

        return self._retry_with_refresh(_do_create)

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

    def _build_client(
        self, creds: Optional[Dict[str, Any]] = None
    ) -> SharePointClient:
        creds = creds or self._creds
        access_token = creds.get("access_token", "")
        site_id = creds.get("site_id")
        return SharePointClient(access_token, site_id=site_id)

    def _retry_with_refresh(
        self, action: Callable[[SharePointClient], Any]
    ) -> Any:
        """Execute *action(client)* and retry once after refreshing the token on 401."""
        client = self._build_client()
        try:
            return action(client)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 401:
                raise

            logger.info(
                "[SharePointSearch] 401 -- attempting token refresh for user %s",
                self.user_id,
            )
            refreshed = self._refresh_credentials()
            if not refreshed:
                raise
            client = self._build_client(refreshed)
            return action(client)

    def _refresh_credentials(self) -> Optional[Dict[str, Any]]:
        refresh_token = self._creds.get("refresh_token")
        if not refresh_token:
            return None
        try:
            token_data = refresh_access_token(refresh_token)
        except Exception as exc:
            logger.warning(
                "[SharePointSearch] Token refresh failed for user %s: %s",
                self.user_id,
                exc,
            )
            return None

        access_token = token_data.get("access_token")
        if not access_token:
            return None

        updated = dict(self._creds)
        updated["access_token"] = access_token
        new_refresh = token_data.get("refresh_token")
        if new_refresh:
            updated["refresh_token"] = new_refresh

        expires_in = token_data.get("expires_in")
        if expires_in:
            updated["expires_in"] = expires_in
            updated["expires_at"] = int(time.time()) + int(expires_in)

        try:
            store_tokens_in_db(self.user_id, updated, "sharepoint")
        except Exception as exc:
            logger.warning(
                "[SharePointSearch] Failed to persist refreshed token for user %s: %s",
                self.user_id,
                exc,
            )
        self._creds = updated
        return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_search_hits(raw_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten Microsoft Graph search response into a list of hit dictionaries."""
    results: List[Dict[str, Any]] = []
    for request_result in raw_response.get("value", []):
        for hit_container in request_result.get("hitsContainers", []):
            for hit in hit_container.get("hits", []):
                resource = hit.get("resource", {})
                entry: Dict[str, Any] = {
                    "hitId": hit.get("hitId"),
                    "rank": hit.get("rank"),
                    "summary": hit.get("summary", ""),
                    "resource": {
                        "id": resource.get("id"),
                        "name": resource.get("name"),
                        "webUrl": resource.get("webUrl"),
                        "lastModifiedDateTime": resource.get(
                            "lastModifiedDateTime"
                        ),
                        "createdBy": resource.get("createdBy"),
                        "lastModifiedBy": resource.get("lastModifiedBy"),
                    },
                }
                results.append(entry)
    return results
