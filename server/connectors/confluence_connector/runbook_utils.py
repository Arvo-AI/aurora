"""Confluence runbook fetch helpers."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from connectors.confluence_connector.auth import refresh_access_token
from connectors.confluence_connector.client import ConfluenceClient, parse_confluence_page_id
from connectors.confluence_connector.runbook_parser import parse_confluence_runbook
from utils.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)


def fetch_confluence_runbook(url: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a Confluence runbook and return normalized content and steps."""
    creds = get_token_data(user_id, "confluence")
    if not creds:
        logger.warning("[PAGERDUTY][RUNBOOK] No Confluence credentials for user %s", user_id)
        return None

    base_url = creds.get("base_url")
    auth_type = (creds.get("auth_type") or "oauth").lower()
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
    if not base_url or not token:
        logger.warning("[PAGERDUTY][RUNBOOK] Missing Confluence credentials for user %s", user_id)
        return None

    if not _host_matches(base_url, url):
        logger.warning("[PAGERDUTY][RUNBOOK] Confluence URL host mismatch: %s", url)
        return None

    page_id = parse_confluence_page_id(url)
    if not page_id:
        logger.warning("[PAGERDUTY][RUNBOOK] Unable to parse Confluence page ID: %s", url)
        return None

    def refresh_confluence_credentials() -> Optional[Dict[str, Any]]:
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            return None
        try:
            token_data = refresh_access_token(refresh_token)
        except Exception as exc:
            logger.warning("[PAGERDUTY][RUNBOOK] Confluence refresh failed for user %s: %s", user_id, exc)
            return None

        access_token = token_data.get("access_token")
        if not access_token:
            return None

        updated_creds = dict(creds)
        updated_creds["access_token"] = access_token
        updated_refresh = token_data.get("refresh_token")
        if updated_refresh:
            updated_creds["refresh_token"] = updated_refresh

        expires_in = token_data.get("expires_in")
        if expires_in:
            updated_creds["expires_in"] = expires_in
            updated_creds["expires_at"] = int(time.time()) + int(expires_in)

        store_tokens_in_db(user_id, updated_creds, "confluence")
        return updated_creds

    try:
        cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
        client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        page_payload = client.get_page(page_id)
        storage_html = (page_payload.get("body") or {}).get("storage", {}).get("value") or ""
        parsed = parse_confluence_runbook(storage_html)
        return {
            "markdown": parsed.get("markdown"),
            "sections": parsed.get("sections"),
            "steps": parsed.get("steps"),
            "page": page_payload,
        }
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401 and auth_type == "oauth":
            refreshed = refresh_confluence_credentials()
            if refreshed:
                token = refreshed.get("access_token")
                cloud_id = refreshed.get("cloud_id") if auth_type == "oauth" else None
                try:
                    client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
                    page_payload = client.get_page(page_id)
                    storage_html = (page_payload.get("body") or {}).get("storage", {}).get("value") or ""
                    parsed = parse_confluence_runbook(storage_html)
                    return {
                        "markdown": parsed.get("markdown"),
                        "sections": parsed.get("sections"),
                        "steps": parsed.get("steps"),
                        "page": page_payload,
                    }
                except Exception as retry_exc:
                    logger.error("[PAGERDUTY][RUNBOOK] Confluence retry failed for %s: %s", url, retry_exc, exc_info=True)
                    return None
            return None
        logger.error("[PAGERDUTY][RUNBOOK] Confluence fetch failed for %s: %s", url, exc, exc_info=True)
        return None
    except Exception as exc:
        logger.error("[PAGERDUTY][RUNBOOK] Confluence fetch failed for %s: %s", url, exc, exc_info=True)
        return None


def _host_matches(base_url: str, page_url: str) -> bool:
    try:
        base_host = urlparse(base_url).netloc.lower()
        page_host = urlparse(page_url).netloc.lower()
        return base_host == page_host
    except Exception:
        return False
