"""OpenSearch REST API client supporting Basic auth and AWS SigV4."""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

OPENSEARCH_TIMEOUT = (5, 20)

# OpenSearch index names / patterns used in URL paths (wildcards and multi-index allowed).
_INDEX_NAME_RE = re.compile(r"^[a-zA-Z0-9_.*+\-,]+$")


class OpenSearchError(Exception):
    """Raised for OpenSearch API errors."""


class OpenSearchClient:
    """Thin REST client for an OpenSearch / Elasticsearch cluster."""

    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        index_pattern: str = "*",
        verify_ssl: bool = True,
        max_retries: int = 2,
    ):
        self.endpoint = OpenSearchClient.normalize_endpoint(endpoint)
        self.username = username
        self.index_pattern = index_pattern
        self.verify_ssl = verify_ssl
        self.max_retries = max_retries
        self._auth = HTTPBasicAuth(username, password)
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_index_name(name: str) -> str:
        """Validate and URL-encode an index name/pattern for path use."""
        if not isinstance(name, str):
            raise OpenSearchError("Invalid OpenSearch index name")
        cleaned = name.strip()
        if not cleaned or not _INDEX_NAME_RE.fullmatch(cleaned):
            raise OpenSearchError("Invalid OpenSearch index name")
        if ".." in cleaned:
            raise OpenSearchError("Invalid OpenSearch index name")
        # Keep wildcard/multi-index chars unescaped for OpenSearch path semantics.
        return quote(cleaned, safe="*+,-._")

    def _url(self, path: str) -> str:
        """Build a request URL from a relative API path (no user host override)."""
        if not path.startswith("/"):
            raise OpenSearchError("Invalid API path: must start with /")
        parsed = urlparse(path)
        if parsed.scheme or parsed.netloc:
            raise OpenSearchError("Invalid API path: must be a relative path")
        if parsed.query or parsed.fragment:
            raise OpenSearchError("Invalid API path: query/fragment not allowed")
        decoded = unquote(parsed.path)
        if "/.." in decoded or "\\" in decoded:
            raise OpenSearchError("Invalid API path: path traversal not allowed")
        normalized = posixpath.normpath(decoded)
        # posixpath.normpath("/") == "/"; reject other non-canonical forms (e.g. "/a/../b").
        if normalized != decoded:
            raise OpenSearchError("Invalid API path: non-canonical path segments not allowed")
        return f"{self.endpoint}{parsed.path}"

    def _http_error_to_opensearch_error(self, exc: requests.HTTPError) -> OpenSearchError:
        """Convert an HTTPError to a descriptive OpenSearchError."""
        status = exc.response.status_code if exc.response is not None else "?"
        if status == 401:
            return OpenSearchError("Authentication failed — check username/password.")
        if status == 403:
            return OpenSearchError("Access forbidden — check cluster permissions.")
        return OpenSearchError(f"HTTP {status}: {exc}")

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = self._url(path)
        last_exc: Exception = RuntimeError("unknown error")
        for attempt in range(max(1, self.max_retries)):
            try:
                resp = self._session.request(
                    method,
                    url,
                    timeout=OPENSEARCH_TIMEOUT,
                    verify=self.verify_ssl,
                    **kwargs,
                )
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError as exc:
                    raise OpenSearchError("OpenSearch returned a non-JSON response.") from exc
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout) as exc:
                last_exc = exc
                logger.warning("[OPENSEARCH] Timeout on attempt %d: %s", attempt + 1, url)
            except requests.exceptions.SSLError as exc:
                raise OpenSearchError(f"SSL error — check the endpoint certificate: {exc}") from exc
            except requests.exceptions.ConnectionError as exc:
                raise OpenSearchError(f"Unable to connect to OpenSearch at {self.endpoint}: {exc}") from exc
            except requests.HTTPError as exc:
                raise self._http_error_to_opensearch_error(exc) from exc
            except requests.RequestException as exc:
                raise OpenSearchError(str(exc)) from exc
        raise OpenSearchError(f"Request failed after {self.max_retries} attempts: {last_exc}") from last_exc

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return cluster health — used for connection validation."""
        return self._request("GET", "/_cluster/health")

    def cluster_info(self) -> Dict[str, Any]:
        """Return cluster name and version info."""
        return self._request("GET", "/")

    def list_indices(self, pattern: Optional[str] = None) -> List[Dict[str, Any]]:
        """List indices matching the pattern."""
        pat = self._sanitize_index_name(pattern or self.index_pattern)
        return self._request(
            "GET",
            f"/_cat/indices/{pat}",
            params={"format": "json", "h": "index,health,status,docs.count,store.size"},
        )

    def search(
        self,
        query: str,
        index: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        size: int = 50,
        timestamp_field: str = "@timestamp",
    ) -> Dict[str, Any]:
        """
        Full-text search across logs.

        Args:
            query: Lucene query string (e.g. 'error AND service:api')
            index: Index or pattern to search (defaults to self.index_pattern)
            start_time: ISO-8601 start time (e.g. 'now-1h')
            end_time: ISO-8601 end time (e.g. 'now')
            size: Max number of hits to return
            timestamp_field: Name of the timestamp field
        """
        raw_idx = index or self.index_pattern
        idx = self._sanitize_index_name(raw_idx)
        must: List[Dict] = [{"query_string": {"query": query, "default_operator": "AND"}}]

        if start_time or end_time:
            time_range: Dict[str, Any] = {}
            if start_time:
                time_range["gte"] = start_time
            if end_time:
                time_range["lte"] = end_time
            must.append({"range": {timestamp_field: time_range}})

        body = {
            "query": {"bool": {"must": must}},
            "size": size,
            "sort": [{timestamp_field: {"order": "desc"}}],
            "_source": True,
        }

        result = self._request("POST", f"/{idx}/_search", json=body)
        hits = result.get("hits", {})
        return {
            "total": hits.get("total", {}).get("value", 0),
            "hits": [h.get("_source", {}) for h in hits.get("hits", [])],
            "index": raw_idx,
            "query": query,
        }

    def get_field_mapping(self, index: Optional[str] = None) -> Dict[str, Any]:
        """Return field mappings to discover available log fields."""
        idx = self._sanitize_index_name(index or self.index_pattern)
        return self._request("GET", f"/{idx}/_mapping")

    @staticmethod
    def normalize_endpoint(raw: str) -> str:
        """Ensure endpoint has a scheme and no trailing slash."""
        url = raw.strip().rstrip("/")
        # Self-hosted OpenSearch may use HTTP on private networks; bare hosts default to HTTPS.
        if not url.startswith(("http://", "https://")):  # NOSONAR
            url = f"https://{url}"
        parsed = urlparse(url)
        if not parsed.netloc:
            raise ValueError(f"Invalid OpenSearch endpoint: {raw!r}")
        return url
