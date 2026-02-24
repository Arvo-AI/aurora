"""Confluence API client and URL helpers."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

# V1 API (deprecated for some endpoints with granular scopes)
DEFAULT_EXPAND = "body.storage,version,space,metadata.labels"
# Shared base for OAuth v1/v2 paths.
OAUTH_API_BASE = "https://api.atlassian.com/ex/confluence"


def is_confluence_cloud_url(base_url: str) -> bool:
    """Return True if the URL looks like a Confluence Cloud hostname."""
    if not base_url:
        return False
    normalized = base_url.strip()
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    hostname = urlparse(normalized).netloc.lower()
    return hostname.endswith(".atlassian.net")


def normalize_confluence_base_url(base_url: str) -> str:
    """Normalize base URL and ensure Cloud URLs include /wiki."""
    if not base_url:
        raise ValueError("Confluence base URL is required")
    normalized = base_url.strip().rstrip("/")
    if "://" not in normalized:
        normalized = f"https://{normalized}"
    if is_confluence_cloud_url(normalized) and not normalized.endswith("/wiki"):
        normalized = f"{normalized}/wiki"
    return normalized


def build_confluence_api_base(base_url: str) -> str:
    """Build the REST API base URL for Cloud or Data Center."""
    normalized = normalize_confluence_base_url(base_url)
    return f"{normalized}/rest/api"


def build_confluence_oauth_api_base(cloud_id: str) -> str:
    """Build the REST API v1 base URL for OAuth requests using cloud ID."""
    return f"{OAUTH_API_BASE}/{cloud_id}/rest/api"


def build_confluence_oauth_api_v2_base(cloud_id: str) -> str:
    """Build the REST API v2 base URL for OAuth requests using cloud ID."""
    return f"{OAUTH_API_BASE}/{cloud_id}/wiki/api/v2"


def parse_confluence_page_id(page_url: str) -> Optional[str]:
    """Extract a Confluence page ID from common URL formats."""
    if not page_url:
        return None

    parsed = urlparse(page_url)
    query = parse_qs(parsed.query)
    if "pageId" in query and query["pageId"]:
        return query["pageId"][0]

    path_parts = [part for part in parsed.path.split("/") if part]
    if "pages" in path_parts:
        idx = path_parts.index("pages")
        if idx + 1 < len(path_parts):
            return path_parts[idx + 1]

    return None




def markdown_to_confluence_storage(markdown_text: str) -> str:
    """Convert basic markdown to Confluence storage format (XHTML).

    Simple regex-based converter for headings, bold, italic, inline code,
    lists (with checkboxes), and paragraphs.  Not a full markdown parser.
    """
    if not markdown_text:
        return ""

    lines = markdown_text.split("\n")
    html_parts: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Headings
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
            html_parts.append(f"<h{level}>{_inline_format(text)}</h{level}>")
            i += 1
            continue

        # List items (group consecutive)
        if re.match(r'^[-*]\s+', line):
            items: List[str] = []
            while i < len(lines) and re.match(r'^[-*]\s+', lines[i]):
                item_line = lines[i]
                checkbox_done = re.match(r'^[-*]\s+\[x\]\s+(.+)$', item_line, re.IGNORECASE)
                checkbox_open = re.match(r'^[-*]\s+\[\s?\]\s+(.+)$', item_line)
                if checkbox_done:
                    items.append(f"<li>\u2611 {_inline_format(checkbox_done.group(1))}</li>")
                elif checkbox_open:
                    items.append(f"<li>\u2610 {_inline_format(checkbox_open.group(1))}</li>")
                else:
                    item_text = re.sub(r'^[-*]\s+', '', item_line)
                    items.append(f"<li>{_inline_format(item_text)}</li>")
                i += 1
            html_parts.append(f"<ul>{''.join(items)}</ul>")
            continue

        # Blank lines \u2014 skip
        if not line.strip():
            i += 1
            continue

        # Regular text \u2192 paragraph
        html_parts.append(f"<p>{_inline_format(line)}</p>")
        i += 1

    return "\n".join(html_parts)


def _inline_format(text: str) -> str:
    """Apply inline markdown formatting (bold, italic, code)."""
    # Bold must come before italic to handle ** vs *
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text

class ConfluenceClient:
    """Minimal Confluence API client for user validation and page retrieval."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        auth_type: str = "oauth",
        timeout: int = 30,
        cloud_id: Optional[str] = None,
    ):
        self.base_url = normalize_confluence_base_url(base_url)
        self.cloud_id = cloud_id
        self.auth_type = auth_type
        # V1 API base (for Data Center/Server or classic scopes)
        self.api_base = (
            build_confluence_oauth_api_base(cloud_id)
            if auth_type == "oauth" and cloud_id
            else build_confluence_api_base(self.base_url)
        )
        # V2 API base (for granular OAuth scopes)
        self.api_v2_base = (
            build_confluence_oauth_api_v2_base(cloud_id)
            if auth_type == "oauth" and cloud_id
            else None
        )
        self.access_token = access_token
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if auth_type not in {"oauth", "pat"}:
            logger.warning(
                "Unknown Confluence auth_type=%s; defaulting to Bearer token.",
                auth_type,
            )

    def _request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("Confluence API request failed: %s %s (%s)", method, url, exc)
            raise

    def _request_v2(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make a request to the v2 API (for granular OAuth scopes)."""
        if not self.api_v2_base:
            raise ValueError("V2 API requires OAuth with cloud_id")
        url = f"{self.api_v2_base}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error(
                "Confluence v2 API request failed: %s %s (%s)", method, url, exc
            )
            raise

    def _request_post(
        self, path: str, json_body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Make a POST request with JSON body to the v1 API."""
        url = f"{self.api_base}{path}"
        headers = {**self.headers, "Content-Type": "application/json"}
        try:
            response = requests.post(
                url,
                headers=headers,
                json=json_body,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error("Confluence API POST failed at path %s: %s", path, type(exc).__name__)
            raise

    def get_current_user(self) -> Dict[str, Any]:
        """Validate credentials by checking access to the API.

        With granular scopes, we validate by listing spaces (read:space:confluence)
        since the Confluence /users/current endpoint requires different permissions.
        """
        if self.api_v2_base:
            # Use spaces endpoint for validation - we have read:space:confluence
            spaces_result = self.list_spaces(limit=1)
            return {
                "type": "oauth_validated",
                "displayName": "Confluence User",
                "spaces_accessible": len(spaces_result.get("results", [])) > 0,
            }
        # Fall back to v1 for Data Center/Server or classic scopes
        return self._request("GET", "/user/current")

    def get_page(self, page_id: str, expand: str = DEFAULT_EXPAND) -> Dict[str, Any]:
        """Fetch a Confluence page by ID using v2 API for OAuth."""
        if self.api_v2_base:
            # V2 API uses different expand format
            return self._request_v2(
                "GET", f"/pages/{page_id}", params={"body-format": "storage"}
            )
        # Fall back to v1 for Data Center/Server
        params = {"expand": expand} if expand else None
        return self._request("GET", f"/content/{page_id}", params=params)

    def search_content(
        self,
        cql: str,
        limit: int = 25,
        expand: str = "version,space,metadata.labels",
        excerpt: bool = True,
    ) -> Dict[str, Any]:
        """Search Confluence content using CQL (v1 API only — no v2 equivalent).

        Args:
            cql: Confluence Query Language expression.
            limit: Maximum results to return (max 25 when expanding body).
            expand: Comma-separated v1 expand fields.
            excerpt: If True, include ``excerpt`` in the expansion.

        Returns:
            Raw JSON response with ``results``, ``start``, ``limit``, ``size``,
            and ``_links`` keys.
        """
        params: Dict[str, Any] = {"cql": cql, "limit": limit}
        if expand:
            full_expand = f"{expand},excerpt" if excerpt else expand
            params["expand"] = full_expand
        return self._request("GET", "/content/search", params=params)

    def list_spaces(self, limit: int = 10) -> Dict[str, Any]:
        """List Confluence spaces."""
        if self.api_v2_base:
            return self._request_v2("GET", "/spaces", params={"limit": limit})
        return self._request("GET", "/space", params={"limit": limit})

    def create_page(
        self,
        space_key: str,
        title: str,
        content_html: str,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Confluence page using the v1 REST API.

        Uses ``/rest/api/content`` (v1) which works for both Cloud and
        Data Center, regardless of OAuth or PAT auth type.

        Args:
            space_key: The Confluence space key (e.g. ``ENG``).
            title: Page title.
            content_html: Page body in Confluence storage format (XHTML).
            parent_id: Optional parent page ID to nest under.

        Returns:
            Dict with ``id`` and ``url`` keys for the created page,
            plus ``_raw`` containing the full API response.
        """
        body: Dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": content_html,
                    "representation": "storage",
                }
            },
        }
        if parent_id:
            body["ancestors"] = [{"id": parent_id}]

        result = self._request_post("/content", body)

        # Extract page URL from response
        page_id = result.get("id", "")
        web_link = ""
        links = result.get("_links", {})
        if "webui" in links:
            base = links.get("base", self.base_url)
            web_link = f"{base}{links['webui']}"

        logger.info("Created Confluence page id=%s", page_id)
        return {"id": page_id, "url": web_link, "_raw": result}
