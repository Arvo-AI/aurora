"""Duplicate a Notion page via the markdown round-trip endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from connectors.notion_connector.client import extract_title

logger = logging.getLogger(__name__)


def _resolve_database_title_key(client: Any, database_id: str) -> str:
    """Return the property name whose ``type == "title"`` on the target DB.

    Notion lets users rename the required title property to anything; hardcoding
    ``"Name"`` breaks duplication into renamed DBs. Fall back to ``"Name"`` only
    if the schema lookup fails so the old behaviour still applies on error.
    """
    try:
        schema = client.get_database(database_id)
    except Exception as exc:
        logger.warning(
            "get_database(%s) failed while duplicating: %s — falling back to 'Name'",
            database_id,
            type(exc).__name__,
        )
        return "Name"
    for key, meta in (schema.get("properties") or {}).items():
        if isinstance(meta, dict) and meta.get("type") == "title":
            return key
    logger.warning(
        "database %s exposes no title property; falling back to 'Name'",
        database_id,
    )
    return "Name"


def _extract_markdown(raw: Any) -> str:
    """Pull the markdown string out of whatever shape Notion returns."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ""
    for key in ("markdown", "content", "text"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    # Some experimental responses wrap the markdown in results[0].markdown
    results = raw.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for key in ("markdown", "content", "text"):
                value = first.get(key)
                if isinstance(value, str):
                    return value
    return ""


def duplicate_page(
    client: Any,
    page_id: str,
    new_parent: Dict[str, Any],
    include_children: bool = True,
    source_page: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Duplicate a Notion page using the markdown round-trip.

    Steps:
      1. Fetch the source page + markdown (skipped if ``source_page`` is supplied).
      2. Create a new page under ``new_parent``, carrying over title/icon/cover.
      3. If ``include_children`` is True, PATCH the new page's markdown with
         ``mode='replace'`` to populate the body.
      4. Return ``{source_page_id, new_page_id, new_page_url}``.
    """
    if source_page is None:
        source_page = client.get_page(page_id)

    try:
        md_raw = client.get_page_markdown(page_id)
    except Exception as exc:
        logger.warning(
            "get_page_markdown failed for %s: %s — duplicating with empty body",
            page_id,
            type(exc).__name__,
        )
        md_raw = {}

    markdown = _extract_markdown(md_raw)
    if include_children and not markdown:
        logger.warning(
            "get_page_markdown returned unrecognised shape for %s; body will be empty",
            page_id,
        )

    title = extract_title(source_page).strip() or "Untitled"
    # Minimal properties: just the title prop. Pages under a page parent use
    # ``title``. Databases let users rename the title column to anything, so
    # look up the actual title property key from the target DB schema.
    properties: Dict[str, Any]
    title_payload = [{"type": "text", "text": {"content": title}}]
    if "database_id" in new_parent:
        title_key = _resolve_database_title_key(client, new_parent["database_id"])
        properties = {title_key: {"title": title_payload}}
    else:
        properties = {"title": {"title": title_payload}}

    icon = source_page.get("icon")
    cover = source_page.get("cover")

    new_page = client.create_page(
        parent=new_parent,
        properties=properties,
        icon=icon,
        cover=cover,
    )

    new_page_id = new_page.get("id", "")
    new_page_url = new_page.get("url", "")

    if include_children and markdown and new_page_id:
        try:
            client.update_page_markdown(new_page_id, markdown, mode="replace")
        except Exception as exc:
            logger.warning(
                "Failed to populate duplicated page markdown (new_id=%s): %s",
                new_page_id,
                type(exc).__name__,
            )

    return {
        "source_page_id": page_id,
        "new_page_id": new_page_id,
        "new_page_url": new_page_url,
    }
