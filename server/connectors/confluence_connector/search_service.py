"""High-level Confluence search service with auth-refresh-retry."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from connectors.confluence_connector.auth import refresh_access_token
from connectors.confluence_connector.client import ConfluenceClient
from connectors.confluence_connector.cql_builder import (
    build_runbook_search_cql,
    build_similar_incidents_cql,
)
from connectors.confluence_connector.runbook_parser import (
    confluence_storage_to_markdown,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)


class ConfluenceSearchService:
    """Searches Confluence via CQL with automatic token refresh on 401."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._creds = get_token_data(user_id, "confluence")
        if not self._creds:
            raise ValueError(f"No Confluence credentials for user {user_id}")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def search_similar_incidents(
        self,
        keywords: List[str],
        service_name: Optional[str] = None,
        error_message: Optional[str] = None,
        spaces: Optional[List[str]] = None,
        labels: Optional[List[str]] = None,
        days_back: int = 365,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        cql = build_similar_incidents_cql(
            keywords=keywords,
            service_name=service_name,
            error_message=error_message,
            spaces=spaces,
            labels=labels,
            days_back=days_back,
        )
        return self._search(cql, limit=max_results)

    def search_runbooks(
        self,
        service_name: str,
        operation: Optional[str] = None,
        spaces: Optional[List[str]] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        cql = build_runbook_search_cql(
            service_name=service_name,
            operation=operation,
            spaces=spaces,
        )
        return self._search(cql, limit=max_results)

    def fetch_page_markdown(
        self, page_id: str, max_length: int = 3000
    ) -> Dict[str, Any]:
        """Fetch a single page and return its content as markdown."""

        def _do_fetch(client: ConfluenceClient) -> Dict[str, Any]:
            page = client.get_page(page_id)
            storage_html = (page.get("body") or {}).get("storage", {}).get(
                "value"
            ) or ""
            md = confluence_storage_to_markdown(storage_html)
            if max_length and len(md) > max_length:
                md = md[:max_length] + "\n\n… [truncated]"
            return {
                "pageId": page.get("id") or page_id,
                "title": page.get("title"),
                "markdown": md,
            }

        return self._retry_with_refresh(_do_fetch)

    # ------------------------------------------------------------------
    # Internal plumbing
    # ------------------------------------------------------------------

    def _build_client(self, creds: Optional[Dict[str, Any]] = None) -> ConfluenceClient:
        creds = creds or self._creds
        base_url = creds.get("base_url", "")
        auth_type = (creds.get("auth_type") or "oauth").lower()
        token = (
            creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
        )
        cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
        return ConfluenceClient(
            base_url, token or "", auth_type=auth_type, cloud_id=cloud_id
        )

    def _search(self, cql: str, limit: int = 25) -> List[Dict[str, Any]]:
        def _do_search(client: ConfluenceClient) -> List[Dict[str, Any]]:
            raw = client.search_content(cql, limit=limit)
            results: List[Dict[str, Any]] = []
            for item in raw.get("results", []):
                entry: Dict[str, Any] = {
                    "pageId": item.get("id"),
                    "title": item.get("title"),
                    "url": item.get("_links", {}).get("webui", ""),
                    "spaceKey": (item.get("space") or {}).get("key"),
                    "lastModified": (item.get("version") or {}).get("when"),
                    "excerpt": item.get("excerpt", ""),
                    "labels": [
                        lbl.get("name")
                        for lbl in (item.get("metadata") or {})
                        .get("labels", {})
                        .get("results", [])
                        if lbl.get("name")
                    ],
                }
                results.append(entry)
            return results

        return self._retry_with_refresh(_do_search)

    def _retry_with_refresh(self, action):
        """Execute *action(client)* and retry once after refreshing the token on 401."""
        client = self._build_client()
        try:
            return action(client)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            auth_type = (self._creds.get("auth_type") or "oauth").lower()
            if status != 401 or auth_type != "oauth":
                raise

            logger.info(
                "[ConfluenceSearch] 401 — attempting token refresh for user %s",
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
                "[ConfluenceSearch] Token refresh failed for user %s: %s",
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

        store_tokens_in_db(self.user_id, updated, "confluence")
        self._creds = updated
        return updated
