"""Microsoft Graph API client for SharePoint operations."""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
USER_AGENT = "ISV|Aurora|SharePointConnector/1.0"


def markdown_to_sharepoint_html(markdown_text: str) -> str:
    """Convert basic markdown to HTML suitable for SharePoint pages.

    Simple regex-based converter for headings, bold, italic, inline code,
    lists, and paragraphs.  Not a full markdown parser.
    """
    if not markdown_text:
        return ""

    lines = markdown_text.split("\n")
    html_parts: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code blocks (```)
        if re.match(r"^```", line):
            lang_match = re.match(r"^```(\w+)?", line)
            lang = lang_match.group(1) if lang_match and lang_match.group(1) else ""
            i += 1
            code_lines: List[str] = []
            while i < len(lines) and not re.match(r"^```\s*$", lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            escaped_body = "\n".join(html.escape(ln) for ln in code_lines)
            if lang:
                html_parts.append(
                    f'<pre data-language="{html.escape(lang)}"><code>{escaped_body}</code></pre>'
                )
            else:
                html_parts.append(f"<pre><code>{escaped_body}</code></pre>")
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            html_parts.append(f"<h{level}>{_inline_format(text)}</h{level}>")
            i += 1
            continue

        # List items (group consecutive)
        if re.match(r"^[-*]\s+", line):
            items: List[str] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                item_text = re.sub(r"^[-*]\s+", "", lines[i])
                items.append(f"<li>{_inline_format(item_text)}</li>")
                i += 1
            html_parts.append(f"<ul>{''.join(items)}</ul>")
            continue

        # Numbered list items
        if re.match(r"^\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                item_text = re.sub(r"^\d+\.\s+", "", lines[i])
                items.append(f"<li>{_inline_format(item_text)}</li>")
                i += 1
            html_parts.append(f"<ol>{''.join(items)}</ol>")
            continue

        # Blank lines -- skip
        if not line.strip():
            i += 1
            continue

        # Regular text -> paragraph
        html_parts.append(f"<p>{_inline_format(line)}</p>")
        i += 1

    return "\n".join(html_parts)


def _inline_format(text: str) -> str:
    """Apply inline markdown formatting (bold, italic, code)."""
    text = html.escape(text, quote=False)
    # Bold must come before italic to handle ** vs *
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def _build_canvas_layout(html_content: str) -> Dict[str, Any]:
    """Build a SharePoint canvas layout structure wrapping HTML content in a single text web part."""
    return {
        "horizontalSections": [
            {
                "layout": "fullWidth",
                "columns": [
                    {
                        "width": 12,
                        "webparts": [
                            {
                                "type": "text",
                                "data": {
                                    "innerHtml": html_content,
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


class SharePointClient:
    """Microsoft Graph API client for SharePoint site, document, and page operations."""

    def __init__(
        self,
        access_token: str,
        site_id: Optional[str] = None,
        timeout: int = 30,
    ):
        self.access_token = access_token
        self.site_id = site_id
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make a request to the Microsoft Graph API.

        Handles HTTP 429 (Too Many Requests) and 503 (Service Unavailable) by
        respecting the ``Retry-After`` header and retrying once.
        """
        url = f"{GRAPH_API_BASE}{path}"
        headers = dict(self.headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                stream=stream,
                timeout=self.timeout,
            )

            # Handle rate limiting / transient errors with a single retry
            if response.status_code in (429, 503):
                retry_after = response.headers.get("Retry-After", "5")
                try:
                    wait_seconds = int(retry_after)
                except (TypeError, ValueError):
                    wait_seconds = 5
                wait_seconds = min(wait_seconds, 60)
                logger.warning(
                    "SharePoint Graph API returned %s; retrying after %ss",
                    response.status_code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    stream=stream,
                    timeout=self.timeout,
                )

            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            logger.error(
                "SharePoint Graph API request failed: %s",
                type(exc).__name__,
            )
            raise

    def _paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_pages: int = 5,
    ) -> List[Dict[str, Any]]:
        """Follow ``@odata.nextLink`` to paginate through Graph API results.

        Args:
            path: Initial Graph API path.
            params: Query parameters for the first request.
            max_pages: Maximum number of pages to fetch (safety limit).

        Returns:
            Aggregated list of ``value`` items from all pages.
        """
        results: List[Dict[str, Any]] = []
        current_path = path
        current_params = params

        next_link: Optional[str] = None
        for _ in range(max_pages):
            resp = self._request("GET", current_path, params=current_params)
            data = resp.json()
            results.extend(data.get("value", []))

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

            # next_link is a full URL; strip the base to get a relative path
            if next_link.startswith(GRAPH_API_BASE):
                current_path = next_link[len(GRAPH_API_BASE):]
            else:
                logger.warning("Unexpected nextLink format; stopping pagination")
                break
            current_params = None  # params are baked into the nextLink

        if next_link:
            logger.warning(
                "Pagination truncated at max_pages=%s; returning %s partial results",
                max_pages,
                len(results),
            )

        return results

    # ------------------------------------------------------------------
    # Site operations
    # ------------------------------------------------------------------

    def get_current_user(self) -> Dict[str, Any]:
        """Validate credentials by fetching the current user profile."""
        resp = self._request("GET", "/me")
        return resp.json()

    def search_sites(self, query: str) -> List[Dict[str, Any]]:
        """Search for SharePoint sites matching the given query."""
        params: Dict[str, Any] = {"search": query} if query else {}
        return self._paginate("/sites", params=params)

    def get_site(self, site_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch a specific SharePoint site by ID."""
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")
        resp = self._request("GET", f"/sites/{sid}")
        return resp.json()

    def list_site_drives(self, site_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List document libraries (drives) for a site."""
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")
        return self._paginate(f"/sites/{sid}/drives")

    # ------------------------------------------------------------------
    # Document / Drive operations
    # ------------------------------------------------------------------

    def list_drive_items(
        self, drive_id: str, folder_path: str = "root"
    ) -> List[Dict[str, Any]]:
        """List children of a drive folder.

        Args:
            drive_id: The drive (document library) ID.
            folder_path: ``"root"`` for top-level or a path like ``"root:/folder"``.
        """
        if folder_path == "root":
            path = f"/drives/{drive_id}/root/children"
        else:
            normalized = folder_path.removeprefix("root:").lstrip("/")
            path = f"/drives/{drive_id}/root:/{normalized}:/children"
        return self._paginate(path)

    def get_drive_item(self, drive_id: str, item_id: str) -> Dict[str, Any]:
        """Fetch metadata for a specific drive item."""
        resp = self._request("GET", f"/drives/{drive_id}/items/{item_id}")
        return resp.json()

    def download_drive_item(self, drive_id: str, item_id: str) -> bytes:
        """Download the content of a drive item as bytes."""
        resp = self._request(
            "GET", f"/drives/{drive_id}/items/{item_id}/content", stream=True
        )
        return resp.content

    def convert_to_pdf(self, drive_id: str, item_id: str) -> bytes:
        """Download a drive item converted to PDF format.

        Uses the Graph API ``/content?format=pdf`` endpoint.
        """
        resp = self._request(
            "GET",
            f"/drives/{drive_id}/items/{item_id}/content",
            params={"format": "pdf"},
            stream=True,
        )
        return resp.content

    # ------------------------------------------------------------------
    # Page operations (read)
    # ------------------------------------------------------------------

    def list_pages(
        self, site_id: Optional[str] = None, top: int = 20
    ) -> List[Dict[str, Any]]:
        """List SharePoint pages for a site.

        Args:
            site_id: SharePoint site ID (falls back to ``self.site_id``).
            top: Maximum number of pages to return per request.
        """
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")
        return self._paginate(f"/sites/{sid}/pages", params={"$top": top})

    def get_page(
        self, site_id: Optional[str] = None, page_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch a SharePoint page with expanded canvas layout.

        Args:
            site_id: SharePoint site ID (falls back to ``self.site_id``).
            page_id: The page ID to retrieve.
        """
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")
        if not page_id:
            raise ValueError("page_id is required")
        resp = self._request(
            "GET",
            f"/sites/{sid}/pages/{page_id}/microsoft.graph.sitePage",
            params={"$expand": "canvasLayout"},
        )
        return resp.json()

    # ------------------------------------------------------------------
    # Page operations (write)
    # ------------------------------------------------------------------

    def create_page(
        self,
        title: str,
        html_content: str,
        site_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new SharePoint page with the given title and HTML content.

        Args:
            title: Page title.
            html_content: HTML content for the page body.
            site_id: SharePoint site ID (falls back to ``self.site_id``).

        Returns:
            The created page resource from the Graph API.
        """
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")

        body: Dict[str, Any] = {
            "name": f"{title}.aspx",
            "title": title,
            "pageLayout": "article",
            "showComments": True,
            "showRecommendedPages": False,
            "canvasLayout": _build_canvas_layout(html_content),
        }

        resp = self._request("POST", f"/sites/{sid}/pages", json_body=body)
        result = resp.json()
        logger.info("Created SharePoint page in site")
        return result

    def publish_page(
        self, site_id: Optional[str] = None, page_id: Optional[str] = None
    ) -> None:
        """Publish a draft SharePoint page.

        Args:
            site_id: SharePoint site ID (falls back to ``self.site_id``).
            page_id: The page ID to publish.
        """
        sid = site_id or self.site_id
        if not sid:
            raise ValueError("site_id is required")
        if not page_id:
            raise ValueError("page_id is required")
        self._request("POST", f"/sites/{sid}/pages/{page_id}/publish")
        logger.info("Published SharePoint page")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        entity_types: Optional[List[str]] = None,
        from_idx: int = 0,
        size: int = 25,
    ) -> Dict[str, Any]:
        """Execute a search query using the Microsoft Graph Search API.

        Args:
            query: The search query string.
            entity_types: List of entity types to search (e.g. ``["driveItem", "site", "page"]``).
            from_idx: Starting index for pagination.
            size: Number of results per page.

        Returns:
            Raw search response from the Graph API.
        """
        if entity_types is None:
            entity_types = ["driveItem", "listItem", "site"]

        body = {
            "requests": [
                {
                    "entityTypes": entity_types,
                    "query": {"queryString": query},
                    "from": from_idx,
                    "size": size,
                }
            ]
        }

        resp = self._request("POST", "/search/query", json_body=body)
        return resp.json()
