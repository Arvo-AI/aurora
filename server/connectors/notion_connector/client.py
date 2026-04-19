"""Notion API client with OAuth + IIT auth, retries, rate limiting, and pagination."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import requests

from connectors.notion_connector import auth as notion_auth
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.cache.redis_client import get_redis_client
from utils.web.redis_rate_limiter import RedisTokenBucket

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
USER_AGENT = "Aurora/NotionConnector"

# Redis-backed global rate limiter — coordinates across all workers/pods.
# Notion's rate limit is ~3 req/s per integration.
_NOTION_BUCKET = RedisTokenBucket(
    key="notion:ratelimit:global", rate_per_sec=3.0, capacity=3
)


class NotionAuthExpiredError(Exception):
    """Raised when the stored Notion credentials cannot be refreshed."""


def extract_title(obj: Dict[str, Any]) -> str:
    """Best-effort title extraction from a Notion page/database object."""
    if not isinstance(obj, dict):
        return ""
    props = obj.get("properties") or {}
    for v in props.values():
        if not isinstance(v, dict):
            continue
        if v.get("type") == "title" and v.get("title"):
            return rich_text_to_plain(v["title"])
    title = obj.get("title")
    if isinstance(title, list):
        return rich_text_to_plain(title)
    if isinstance(title, str):
        return title
    return obj.get("name") or ""


def rich_text_to_plain(rt: Any) -> str:
    """Flatten a Notion rich_text array into a plain string."""
    if not isinstance(rt, list):
        return ""
    return "".join(
        seg.get("plain_text", "") for seg in rt if isinstance(seg, dict)
    )


class NotionClient:
    """Notion API client.

    Handles OAuth token refresh, Internal Integration Token (IIT) auth,
    rate limiting, 429/5xx retries, and paginated endpoints. All public
    methods are wrapped through :meth:`_retry_with_refresh` so any 401 is
    retried with a freshly refreshed token.
    """

    def __init__(self, user_id: str, timeout: int = 30):
        self.user_id = user_id
        self.timeout = timeout
        self._user_email_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._users_fully_cached: bool = False

        creds = get_token_data(user_id, "notion")
        if not creds:
            raise ValueError(f"No Notion credentials for user {user_id}")

        self.creds: Dict[str, Any] = dict(creds)
        self.token_type = (self.creds.get("type") or "oauth").lower()
        if self.token_type == "iit":
            self.access_token = self.creds.get("token") or self.creds.get(
                "access_token", ""
            )
        else:
            self.access_token = self.creds.get("access_token", "")
        if not self.access_token:
            raise ValueError(
                f"Notion credentials for user {user_id} missing access token"
            )

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self, json_body: bool = True) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Notion-Version": NOTION_VERSION,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
        _headers_override: Optional[Dict[str, str]] = None,
        _files: Optional[Any] = None,
        _data: Optional[Any] = None,
    ) -> Any:
        """Issue a request against the Notion API with retries.

        Rate-limit + 5xx handling:
          - 429: sleep for ``Retry-After`` (capped at 60s) and retry once.
          - 5xx: exponential-backoff retry once.
        """
        if not _NOTION_BUCKET.acquire(timeout=10.0):
            raise RuntimeError(
                f"Notion rate limit exceeded — please retry in a few seconds (path={path})"
            )

        url = f"{NOTION_API_BASE}{path}"
        headers = _headers_override if _headers_override is not None else self._headers(
            json_body=json is not None
        )

        def _do() -> requests.Response:
            return requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                files=_files,
                data=_data,
                timeout=self.timeout,
            )

        response = _do()

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "1")
            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = 1.0
            wait_seconds = min(max(wait_seconds, 0.1), 60.0)
            logger.warning(
                "Notion 429 rate limit; retrying after %.1fs (path=%s)",
                wait_seconds,
                path,
            )
            time.sleep(wait_seconds)
            response = _do()

        elif 500 <= response.status_code < 600:
            logger.warning(
                "Notion %s on %s; retrying once with backoff",
                response.status_code,
                path,
            )
            time.sleep(1.5)
            response = _do()

        response.raise_for_status()
        if raw:
            return response
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            logger.warning("Notion response for %s is not valid JSON", path)
            return {}

    def _retry_with_refresh(self, fn: Callable[[], Any]) -> Any:
        """Invoke ``fn()``; on 401 refresh OAuth token and retry once.

        Uses a Redis lock per user to prevent concurrent refresh races
        across workers. For IIT-type tokens or when refresh fails, raises
        :class:`NotionAuthExpiredError`.
        """
        try:
            return fn()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 401:
                raise

            if self.token_type != "oauth":
                logger.info(
                    "[Notion] 401 with IIT token for user %s; cannot refresh",
                    self.user_id,
                )
                raise NotionAuthExpiredError(
                    "Notion IIT token is invalid or revoked"
                ) from exc

            refresh_token = self.creds.get("refresh_token")
            if not refresh_token:
                logger.info(
                    "[Notion] 401 without refresh_token for user %s",
                    self.user_id,
                )
                raise NotionAuthExpiredError(
                    "Notion OAuth session expired and no refresh token available"
                ) from exc

            # Distributed lock prevents two pods from refreshing the same
            # user's token simultaneously (loser would overwrite with stale).
            lock_key = f"notion:refresh_lock:{self.user_id}"
            rc = get_redis_client()
            acquired = False
            if rc:
                acquired = rc.set(lock_key, "1", nx=True, ex=30)
                if not acquired:
                    # Another worker is refreshing — wait briefly then reload
                    time.sleep(2)
                    reloaded = get_token_data(self.user_id, "notion")
                    if reloaded and reloaded.get("access_token") != self.access_token:
                        self.creds = reloaded
                        self.access_token = reloaded["access_token"]
                        return fn()
                    raise NotionAuthExpiredError(
                        "Concurrent refresh in progress — retry"
                    ) from exc

            try:
                new_token_data = notion_auth.refresh_access_token(refresh_token)

                new_access = new_token_data.get("access_token")
                if not new_access:
                    raise NotionAuthExpiredError(
                        "Notion OAuth refresh returned no access_token"
                    )

                updated = dict(self.creds)
                updated["access_token"] = new_access
                if new_token_data.get("refresh_token"):
                    updated["refresh_token"] = new_token_data["refresh_token"]
                if new_token_data.get("expires_in"):
                    updated["expires_in"] = new_token_data["expires_in"]
                    updated["expires_at"] = new_token_data.get("expires_at") or (
                        int(time.time()) + int(new_token_data["expires_in"])
                    )

                try:
                    store_tokens_in_db(self.user_id, updated, "notion")
                except Exception as store_exc:
                    logger.error(
                        "[Notion] Failed to persist refreshed token for user %s: %s",
                        self.user_id,
                        store_exc,
                    )
                    raise NotionAuthExpiredError(
                        "Notion token refreshed but failed to persist — "
                        "re-authenticate to restore access"
                    ) from store_exc

                self.creds = updated
                self.access_token = new_access
            except NotionAuthExpiredError:
                raise
            except Exception as refresh_exc:
                logger.warning(
                    "[Notion] Token refresh failed for user %s: %s",
                    self.user_id,
                    refresh_exc,
                )
                raise NotionAuthExpiredError(
                    "Notion OAuth refresh failed"
                ) from refresh_exc
            finally:
                if rc and acquired:
                    rc.delete(lock_key)

            return fn()

    # ------------------------------------------------------------------
    # Helpers for "new" endpoints (data sources, views, emojis) that may
    # 404 on older workspaces.
    # ------------------------------------------------------------------

    def _graceful_404(
        self, fn: Callable[[], Any], feature: str
    ) -> Dict[str, Any]:
        try:
            return fn()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                logger.info(
                    "Notion %s endpoint returned 404 (feature not available)",
                    feature,
                )
                return {"error": f"{feature}_not_available"}
            # Some endpoints return 400 when the API version is too old or the
            # feature is not available for this workspace/plan.  Treat these
            # the same as 404 for optional-feature guards.
            if status == 400:
                body = {}
                try:
                    body = exc.response.json() if exc.response is not None else {}
                except Exception:
                    pass  # Malformed JSON in error response — use empty body
                code = body.get("code", "")
                message = (body.get("message") or "").lower()
                is_feature_gate = (
                    code == "invalid_request_url"
                    or (code in ("validation_error", "invalid_request")
                        and ("not available" in message or "not supported" in message
                             or "does not exist" in message))
                )
                if is_feature_gate:
                    logger.info(
                        "Notion %s endpoint returned 400/%s (feature not available for this API version or plan)",
                        feature,
                        code,
                    )
                    return {"error": f"{feature}_not_available"}
            raise

    # ==================================================================
    # Search
    # ==================================================================

    def search(
        self,
        query: str,
        *,
        filter_types: Optional[List[str]] = None,
        max_results: int = 25,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run a Notion global search.

        ``filter_types`` may be ``["page"]``, ``["database"]`` or both; when a
        single type is provided, a filter is applied. When both or none, the
        filter is omitted.
        """
        def _do() -> Dict[str, Any]:
            body: Dict[str, Any] = {
                "query": query or "",
                "page_size": max(1, min(int(max_results), 100)),
            }
            if filter_types and len(filter_types) == 1:
                value = filter_types[0]
                if value in ("page", "database"):
                    body["filter"] = {"value": value, "property": "object"}
            if start_cursor is not None:
                body["start_cursor"] = start_cursor
            return self._request("POST", "/search", json=body)

        return self._retry_with_refresh(_do)

    def search_pages(self, query: str, max_results: int = 25, start_cursor: Optional[str] = None) -> Dict[str, Any]:
        return self.search(query, filter_types=["page"], max_results=max_results, start_cursor=start_cursor)

    def search_databases(self, query: str, max_results: int = 25, start_cursor: Optional[str] = None) -> Dict[str, Any]:
        return self.search(query, filter_types=["database"], max_results=max_results, start_cursor=start_cursor)

    # ==================================================================
    # Pages
    # ==================================================================

    def get_page(self, page_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/pages/{page_id}")
        )

    def get_page_markdown(self, page_id: str) -> Dict[str, Any]:
        """Fetch a page's markdown via the /pages/{id}/markdown endpoint."""
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/pages/{page_id}/markdown")
        )

    def update_page_markdown(
        self, page_id: str, markdown: str, mode: str = "append"
    ) -> Dict[str, Any]:
        """Insert or replace a page's body via Notion's markdown endpoint.

        ``mode="append"`` appends to the bottom of the page. ``mode="replace"``
        deletes all existing block children on the page first, then inserts the
        new content so the page body is fully replaced.
        """
        if mode not in ("append", "replace"):
            raise ValueError("mode must be 'append' or 'replace'")

        if mode == "replace":
            self._delete_all_block_children(page_id)

        body = {
            "type": "insert_content",
            "insert_content": {"content": markdown},
        }
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH", f"/pages/{page_id}/markdown", json=body
            )
        )

    def _delete_all_block_children(self, page_id: str) -> None:
        """Delete every top-level block child of a page.

        Iterates through all children (handling pagination) and issues a
        DELETE for each block. Used by :meth:`update_page_markdown` with
        ``mode="replace"`` to clear existing content before inserting new.
        """
        block_ids = [
            block["id"]
            for block in self.iter_block_children(page_id)
            if block.get("id")
        ]
        failed: List[str] = []
        for block_id in block_ids:
            try:
                self.delete_block(block_id)
            except NotionAuthExpiredError:
                raise
            except Exception as exc:
                logger.warning(
                    "Failed to delete block %s while clearing page %s: %s",
                    block_id,
                    page_id,
                    exc,
                )
                failed.append(block_id)
        if failed:
            raise RuntimeError(
                f"Failed to delete {len(failed)}/{len(block_ids)} blocks "
                f"from page {page_id}"
            )

    def trash_page(self, page_id: str) -> Dict[str, Any]:
        """Archive (soft-delete) a page."""
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH", f"/pages/{page_id}", json={"archived": True}
            )
        )

    def move_page(self, page_id: str, new_parent: Dict[str, Any]) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH", f"/pages/{page_id}", json={"parent": new_parent}
            )
        )

    def create_page(
        self,
        parent: Dict[str, Any],
        properties: Dict[str, Any],
        *,
        icon: Optional[Dict[str, Any]] = None,
        cover: Optional[Dict[str, Any]] = None,
        children: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"parent": parent, "properties": properties}
        if icon is not None:
            body["icon"] = icon
        if cover is not None:
            body["cover"] = cover
        if children is not None:
            body["children"] = children
        return self._retry_with_refresh(
            lambda: self._request("POST", "/pages", json=body)
        )

    def update_page(
        self,
        page_id: str,
        *,
        properties: Optional[Dict[str, Any]] = None,
        icon: Optional[Dict[str, Any]] = None,
        cover: Optional[Dict[str, Any]] = None,
        archived: Optional[bool] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if properties is not None:
            body["properties"] = properties
        if icon is not None:
            body["icon"] = icon
        if cover is not None:
            body["cover"] = cover
        if archived is not None:
            body["archived"] = archived
        return self._retry_with_refresh(
            lambda: self._request("PATCH", f"/pages/{page_id}", json=body)
        )

    # ==================================================================
    # Databases
    # ==================================================================

    def get_database(self, database_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/databases/{database_id}")
        )

    def query_database(
        self,
        database_id: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
        max_results: int = 25,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"page_size": max(1, min(int(max_results), 100))}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._request(
                "POST", f"/databases/{database_id}/query", json=body
            )
        )

    def create_database(
        self,
        parent: Dict[str, Any],
        title: List[Dict[str, Any]],
        properties: Dict[str, Any],
        icon: Optional[Dict[str, Any]] = None,
        cover: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "parent": parent,
            "title": title,
            "properties": properties,
        }
        if icon is not None:
            body["icon"] = icon
        if cover is not None:
            body["cover"] = cover
        return self._retry_with_refresh(
            lambda: self._request("POST", "/databases", json=body)
        )

    def update_database(self, database_id: str, **updates: Any) -> Dict[str, Any]:
        body = {k: v for k, v in updates.items() if v is not None}
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH", f"/databases/{database_id}", json=body
            )
        )

    def update_database_properties(
        self, database_id: str, properties: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH",
                f"/databases/{database_id}",
                json={"properties": properties},
            )
        )

    # ==================================================================
    # Data Sources (new API - graceful 404)
    # ==================================================================

    def create_data_source(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("POST", "/data_sources", json=payload),
                "data_sources",
            )
        )

    def get_data_source(self, data_source_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("GET", f"/data_sources/{data_source_id}"),
                "data_sources",
            )
        )

    def update_data_source(
        self, data_source_id: str, **updates: Any
    ) -> Dict[str, Any]:
        body = {k: v for k, v in updates.items() if v is not None}
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "PATCH", f"/data_sources/{data_source_id}", json=body
                ),
                "data_sources",
            )
        )

    def update_data_source_properties(
        self, data_source_id: str, properties: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "PATCH",
                    f"/data_sources/{data_source_id}",
                    json={"properties": properties},
                ),
                "data_sources",
            )
        )

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
        max_results: int = 25,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"page_size": max(1, min(int(max_results), 100))}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "POST",
                    f"/data_sources/{data_source_id}/query",
                    json=body,
                ),
                "data_sources",
            )
        )

    def list_data_source_templates(
        self, data_source_id: str
    ) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "GET", f"/data_sources/{data_source_id}/templates"
                ),
                "data_sources",
            )
        )

    # ==================================================================
    # Blocks
    # ==================================================================

    def get_block(self, block_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/blocks/{block_id}")
        )

    def update_block(
        self, block_id: str, block: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("PATCH", f"/blocks/{block_id}", json=block)
        )

    def delete_block(self, block_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("DELETE", f"/blocks/{block_id}")
        )

    def get_block_children(
        self,
        block_id: str,
        *,
        start_cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._request(
                "GET", f"/blocks/{block_id}/children", params=params
            )
        )

    def append_block_children(
        self, block_id: str, children: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request(
                "PATCH",
                f"/blocks/{block_id}/children",
                json={"children": children},
            )
        )

    def iter_block_children(
        self, block_id: str, max_pages: int = 20
    ) -> Iterator[Dict[str, Any]]:
        """Yield block children across paginated pages."""
        cursor: Optional[str] = None
        for page_idx in range(max_pages):
            page = self.get_block_children(block_id, start_cursor=cursor)
            for block in page.get("results", []):
                yield block
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                return
        logger.warning(
            "iter_block_children truncated at max_pages=%s for block %s",
            max_pages,
            block_id,
        )

    # ==================================================================
    # Views (new API - graceful 404)
    # ==================================================================

    def create_view(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("POST", "/views", json=payload), "views"
            )
        )

    def get_view(self, view_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("GET", f"/views/{view_id}"), "views"
            )
        )

    def update_view(self, view_id: str, **updates: Any) -> Dict[str, Any]:
        body = {k: v for k, v in updates.items() if v is not None}
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("PATCH", f"/views/{view_id}", json=body),
                "views",
            )
        )

    def delete_view(self, view_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("DELETE", f"/views/{view_id}"), "views"
            )
        )

    def list_database_views(self, database_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "GET", f"/databases/{database_id}/views"
                ),
                "views",
            )
        )

    def query_view(
        self,
        view_id: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
        max_results: int = 25,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"page_size": max(1, min(int(max_results), 100))}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request(
                    "POST", f"/views/{view_id}/query", json=body
                ),
                "views",
            )
        )

    def paginate_view_query(
        self, view_id: str, max_pages: int = 10, page_size: int = 100
    ) -> Iterator[Dict[str, Any]]:
        cursor: Optional[str] = None
        for _ in range(max_pages):
            page = self.query_view(
                view_id, max_results=page_size, start_cursor=cursor
            )
            if isinstance(page, dict) and page.get("error") == "views_not_available":
                return
            for item in page.get("results", []):
                yield item
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ==================================================================
    # Users
    # ==================================================================

    def list_users(
        self,
        *,
        page_size: int = 100,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._request("GET", "/users", params=params)
        )

    def get_user(self, user_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/users/{user_id}")
        )

    def get_self(self) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", "/users/me")
        )

    def _walk_users(self, *, early_exit_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Paginate /users (up to 500 pages / ~50k users), populate the email
        cache, and optionally short-circuit on a target email match.

        Sets ``_users_fully_cached=True`` when the walk reaches the end of
        results. Returns the matched user when ``early_exit_email`` is given
        and found; otherwise returns None.
        """
        cursor: Optional[str] = None
        max_pages = 500
        for page_idx in range(max_pages):
            page = self.list_users(start_cursor=cursor)
            for user in page.get("results", []):
                person = user.get("person") or {}
                candidate = (person.get("email") or "").strip().lower()
                if candidate:
                    self._user_email_cache[candidate] = user
                if early_exit_email and candidate == early_exit_email:
                    return user
            if not page.get("has_more"):
                self._users_fully_cached = True
                return None
            cursor = page.get("next_cursor")
            if not cursor:
                self._users_fully_cached = True
                return None
        logger.warning(
            "[Notion] _walk_users reached page limit (%s pages, ~%s users) "
            "without exhausting results for user %s%s",
            max_pages,
            max_pages * 100,
            self.user_id,
            f"; target email not found: {early_exit_email}" if early_exit_email else "",
        )
        return None

    def prime_user_cache(self) -> None:
        """Walk /users once and populate the email→user cache.

        Callers that know they'll look up multiple emails (e.g. action-item
        sync) should prime upfront — turns N × paginate into one.
        """
        if self._users_fully_cached:
            return
        self._walk_users()

    def find_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Iterate /users and return the first person whose email matches."""
        if not email:
            return None
        needle = email.strip().lower()
        if needle in self._user_email_cache:
            return self._user_email_cache[needle]
        if self._users_fully_cached:
            return None

        match = self._walk_users(early_exit_email=needle)
        if match is not None:
            return match
        # Only memoize the miss if the walk was exhaustive; otherwise a later
        # lookup might legitimately find the user on a page we never reached.
        if self._users_fully_cached:
            self._user_email_cache[needle] = None
        return None

    # ==================================================================
    # Comments
    # ==================================================================

    def create_comment(
        self,
        parent: Optional[Dict[str, Any]] = None,
        discussion_id: Optional[str] = None,
        rich_text: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if rich_text is None:
            raise ValueError("rich_text is required for create_comment")
        if not parent and not discussion_id:
            raise ValueError("Either parent or discussion_id must be provided")
        body: Dict[str, Any] = {"rich_text": rich_text}
        if parent is not None:
            body["parent"] = parent
        if discussion_id is not None:
            body["discussion_id"] = discussion_id
        return self._retry_with_refresh(
            lambda: self._request("POST", "/comments", json=body)
        )

    def list_comments(
        self,
        block_id: str,
        *,
        start_cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "block_id": block_id,
            "page_size": max(1, min(int(page_size), 100)),
        }
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._request("GET", "/comments", params=params)
        )

    def get_comment(self, comment_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/comments/{comment_id}")
        )

    # ==================================================================
    # Emojis (new API - graceful 404)
    # ==================================================================

    def list_emojis(self) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._graceful_404(
                lambda: self._request("GET", "/emojis"), "emojis"
            )
        )

    # ==================================================================
    # File uploads
    # ==================================================================

    def create_file_upload(
        self,
        *,
        mode: str = "single_part",
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        number_of_parts: Optional[int] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"mode": mode}
        if filename is not None:
            body["filename"] = filename
        if content_type is not None:
            body["content_type"] = content_type
        if number_of_parts is not None:
            body["number_of_parts"] = number_of_parts
        return self._retry_with_refresh(
            lambda: self._request("POST", "/file_uploads", json=body)
        )

    def send_file_upload(
        self,
        upload_id: str,
        file_bytes: bytes,
        *,
        part_number: Optional[int] = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload the file bytes as multipart/form-data."""
        files = {
            "file": (
                filename or "upload.bin",
                file_bytes,
                content_type or "application/octet-stream",
            )
        }
        data: Dict[str, Any] = {}
        if part_number is not None:
            data["part_number"] = str(part_number)

        # For multipart we must NOT set Content-Type ourselves — requests sets
        # it with the boundary. Build headers inside the callable so a token
        # refresh produces fresh Authorization on retry.
        def _do() -> Dict[str, Any]:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Notion-Version": NOTION_VERSION,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
            return self._request(
                "POST",
                f"/file_uploads/{upload_id}/send",
                _headers_override=headers,
                _files=files,
                _data=data,
            )

        return self._retry_with_refresh(_do)

    def complete_file_upload(self, upload_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request(
                "POST", f"/file_uploads/{upload_id}/complete"
            )
        )

    def get_file_upload(self, upload_id: str) -> Dict[str, Any]:
        return self._retry_with_refresh(
            lambda: self._request("GET", f"/file_uploads/{upload_id}")
        )

    def list_file_uploads(
        self,
        *,
        page_size: int = 25,
        start_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return self._retry_with_refresh(
            lambda: self._request("GET", "/file_uploads", params=params)
        )
