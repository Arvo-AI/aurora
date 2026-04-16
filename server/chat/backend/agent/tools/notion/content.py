"""Notion content chat tools: search, fetch, pages, blocks."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.notion_connector import duplicator
from connectors.notion_connector.url_parser import parse_notion_url

from .common import (
    build_cover,
    build_icon,
    build_title_property,
    extract_title,
    notion_tool_error,
    run_notion_tool,
)


# ===========================================================================
# Search / fetch
# ===========================================================================


class NotionSearchArgs(BaseModel):
    query: str = Field(description="Workspace-wide search query (page/database titles and text).")
    types: Optional[List[str]] = Field(
        default=None,
        description="Filter to one object type: ['page'] or ['database']. Omit or pass both for a mixed search.",
    )
    max_results: int = Field(default=10, ge=1, le=100, description="Max results to return (1-100).")


class NotionFetchArgs(BaseModel):
    url_or_id: str = Field(
        description="Full Notion URL or bare page/database/block ID (UUID, with or without dashes)."
    )
    max_length: int = Field(
        default=5000,
        ge=100,
        le=50000,
        description="Max markdown characters to return for page fetches (truncated with a marker).",
    )


def notion_search(
    query: str,
    types: Optional[List[str]] = None,
    max_results: int = 10,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search Notion workspace for pages and/or databases."""
    _ = session_id

    filter_types: Optional[List[str]] = None
    if types and len(types) == 1 and types[0] in ("page", "database"):
        filter_types = [types[0]]

    def _do(client: Any) -> Dict[str, Any]:
        raw = client.search(
            query=query,
            filter_types=filter_types,
            max_results=max_results,
        )
        results = [
            {
                "id": item.get("id"),
                "object": item.get("object"),
                "title": extract_title(item),
                "url": item.get("url"),
                "last_edited_time": item.get("last_edited_time"),
            }
            for item in raw.get("results") or []
        ]
        return {"count": len(results), "results": results}

    return run_notion_tool(user_id, _do)


def notion_fetch(
    url_or_id: str,
    max_length: int = 5000,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a Notion page/database/block by URL or ID."""
    _ = session_id

    try:
        parsed = parse_notion_url(url_or_id)
    except Exception as exc:
        return notion_tool_error(
            f"Could not parse Notion URL/ID: {exc}", code="bad_input"
        )

    def _do(client: Any) -> Dict[str, Any]:
        if "page_id" in parsed:
            page = client.get_page(parsed["page_id"])
            try:
                md_resp = client.get_page_markdown(parsed["page_id"])
                if isinstance(md_resp, dict):
                    markdown = (
                        md_resp.get("markdown")
                        or md_resp.get("content")
                        or md_resp.get("text")
                        or ""
                    )
                else:
                    markdown = str(md_resp)
            except Exception:
                markdown = ""
            if max_length and markdown and len(markdown) > max_length:
                markdown = markdown[:max_length] + "\n\n... [truncated]"
            return {
                "type": "page",
                "id": page.get("id"),
                "url": page.get("url"),
                "title": extract_title(page),
                "markdown": markdown or "",
            }
        if "database_id" in parsed:
            db = client.get_database(parsed["database_id"])
            return {
                "type": "database",
                "id": db.get("id"),
                "url": db.get("url"),
                "title": extract_title(db),
                "properties": list((db.get("properties") or {}).keys()),
            }
        if "block_id" in parsed:
            blk = client.get_block(parsed["block_id"])
            return {
                "type": "block",
                "id": blk.get("id"),
                "block_type": blk.get("type"),
            }
        return {"error": "Could not parse Notion URL/ID"}

    return run_notion_tool(user_id, _do)


# ===========================================================================
# Pages
# ===========================================================================


class PageSpec(BaseModel):
    parent_page_id: Optional[str] = Field(
        default=None,
        description="ID of the parent page (mutually exclusive with parent_database_id).",
    )
    parent_database_id: Optional[str] = Field(
        default=None,
        description="ID of the parent database (mutually exclusive with parent_page_id).",
    )
    title: str = Field(description="Title for the new page.")
    markdown: Optional[str] = Field(
        default=None,
        description="Optional markdown body to populate the new page (replace mode).",
    )
    icon: Optional[str] = Field(
        default=None,
        description="Optional icon: emoji character (e.g. '🚀') or image URL.",
    )
    cover: Optional[str] = Field(
        default=None, description="Optional cover image URL."
    )
    properties: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw Notion properties payload (only used when parent is a database). Overrides title-based property building.",
    )


class NotionCreatePagesArgs(BaseModel):
    pages: List[PageSpec] = Field(
        description="List of page specifications to create. Each entry must set exactly one of parent_page_id / parent_database_id."
    )


class NotionUpdatePageArgs(BaseModel):
    page_id: str = Field(description="ID of the Notion page to update.")
    properties: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw Notion properties payload to update on the page.",
    )
    icon: Optional[str] = Field(
        default=None, description="Emoji char or image URL to set as icon."
    )
    cover: Optional[str] = Field(
        default=None, description="Image URL to set as the cover."
    )
    archived: Optional[bool] = Field(
        default=None,
        description="If true, archives (trashes) the page; if false, restores.",
    )
    markdown: Optional[str] = Field(
        default=None,
        description="Optional markdown body. Applied via update_page_markdown.",
    )
    markdown_mode: str = Field(
        default="append",
        description="'append' adds to end of page body; 'replace' overwrites it.",
    )


class NotionAppendToPageArgs(BaseModel):
    page_id: str = Field(description="ID of the page to append to.")
    markdown: str = Field(description="Markdown content to append to the page body.")


class NotionMovePagesArgs(BaseModel):
    page_ids: List[str] = Field(description="IDs of pages to move.")
    new_parent_id: str = Field(
        description="ID of the new parent page."
    )


class NotionDuplicatePageArgs(BaseModel):
    page_id: str = Field(description="ID of the source page to duplicate.")
    new_parent_id: Optional[str] = Field(
        default=None,
        description="Optional destination parent page ID. If omitted, duplicates under the source page's existing parent.",
    )


class NotionTrashPageArgs(BaseModel):
    page_id: str = Field(description="ID of the page to archive (soft delete).")


def notion_create_pages(
    pages: List[Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create one or more Notion pages under a page or database parent."""
    _ = session_id

    if not pages:
        return notion_tool_error("pages list is empty", code="bad_input")

    specs: List[Dict[str, Any]] = []
    for p in pages:
        if hasattr(p, "model_dump"):
            specs.append(p.model_dump())
        elif hasattr(p, "dict"):
            specs.append(p.dict())
        elif isinstance(p, dict):
            specs.append(p)
        else:
            return notion_tool_error(
                f"Unsupported page spec type: {type(p).__name__}", code="bad_input"
            )

    def _do(client: Any) -> Dict[str, Any]:
        created: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for spec in specs:
            parent_page_id = spec.get("parent_page_id")
            parent_database_id = spec.get("parent_database_id")
            if bool(parent_page_id) == bool(parent_database_id):
                errors.append(
                    {
                        "title": spec.get("title"),
                        "error": "exactly one of parent_page_id / parent_database_id must be set",
                    }
                )
                continue

            title = spec.get("title") or "Untitled"
            explicit_props = spec.get("properties")

            if parent_database_id:
                parent = {"database_id": parent_database_id}
                if explicit_props:
                    properties = explicit_props
                else:
                    properties = build_title_property(title, for_database=True)
            else:
                parent = {"page_id": parent_page_id}
                properties = build_title_property(title, for_database=False)

            try:
                new_page = client.create_page(
                    parent=parent,
                    properties=properties,
                    icon=build_icon(spec.get("icon")),
                    cover=build_cover(spec.get("cover")),
                )
            except Exception as exc:
                errors.append({"title": title, "error": str(exc)})
                continue

            new_id = new_page.get("id")
            markdown = spec.get("markdown")
            if markdown and new_id:
                try:
                    client.update_page_markdown(new_id, markdown, mode="replace")
                except Exception as exc:
                    errors.append(
                        {
                            "title": title,
                            "id": new_id,
                            "error": f"page created but markdown population failed: {exc}",
                        }
                    )

            created.append(
                {
                    "id": new_id,
                    "url": new_page.get("url"),
                    "title": extract_title(new_page) or title,
                }
            )
        return {"created": created, "errors": errors, "count": len(created)}

    return run_notion_tool(user_id, _do)


def notion_update_page(
    page_id: str,
    properties: Optional[Dict[str, Any]] = None,
    icon: Optional[str] = None,
    cover: Optional[str] = None,
    archived: Optional[bool] = None,
    markdown: Optional[str] = None,
    markdown_mode: str = "append",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update page attributes and/or body."""
    _ = session_id

    if markdown_mode not in ("append", "replace"):
        return notion_tool_error(
            "markdown_mode must be 'append' or 'replace'", code="bad_input"
        )

    def _do(client: Any) -> Dict[str, Any]:
        attrs_touched = any(
            v is not None for v in (properties, icon, cover, archived)
        )
        updated: Dict[str, Any] = {}
        if attrs_touched:
            updated = client.update_page(
                page_id,
                properties=properties,
                icon=build_icon(icon),
                cover=build_cover(cover),
                archived=archived,
            )
        if markdown:
            client.update_page_markdown(page_id, markdown, mode=markdown_mode)
        return {
            "page_id": page_id,
            "updated_attributes": bool(attrs_touched),
            "markdown_mode": markdown_mode if markdown else None,
            "url": updated.get("url") if isinstance(updated, dict) else None,
            "markdown_applied": markdown is not None,
        }

    return run_notion_tool(user_id, _do)


def notion_append_to_page(
    page_id: str,
    markdown: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Append markdown to the end of a Notion page."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        client.update_page_markdown(page_id, markdown, mode="append")
        return {"page_id": page_id, "mode": "append", "applied": True}

    return run_notion_tool(user_id, _do)


def notion_move_pages(
    page_ids: List[str],
    new_parent_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Move one or more pages under a new parent page."""
    _ = session_id

    if not page_ids:
        return notion_tool_error("page_ids is empty", code="bad_input")
    if not new_parent_id:
        return notion_tool_error("new_parent_id is required", code="bad_input")

    def _do(client: Any) -> Dict[str, Any]:
        moved: List[str] = []
        errors: List[Dict[str, Any]] = []
        new_parent = {"page_id": new_parent_id}
        for pid in page_ids:
            try:
                client.move_page(pid, new_parent)
                moved.append(pid)
            except Exception as exc:
                errors.append({"page_id": pid, "error": str(exc)})
        return {
            "moved": moved,
            "errors": errors,
            "count": len(moved),
            "new_parent_id": new_parent_id,
        }

    return run_notion_tool(user_id, _do)


def notion_duplicate_page(
    page_id: str,
    new_parent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Duplicate a Notion page via markdown round-trip."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        source = None
        if new_parent_id:
            new_parent: Dict[str, Any] = {"page_id": new_parent_id}
        else:
            source = client.get_page(page_id)
            parent = source.get("parent") or {}
            parent_type = parent.get("type")
            if parent_type == "page_id" and parent.get("page_id"):
                new_parent = {"page_id": parent["page_id"]}
            elif parent_type == "database_id" and parent.get("database_id"):
                new_parent = {"database_id": parent["database_id"]}
            elif parent_type == "workspace":
                new_parent = {"workspace": True}
            else:
                return {
                    "error": "Could not derive parent from source page; please pass new_parent_id explicitly."
                }

        return duplicator.duplicate_page(
            client, page_id, new_parent, include_children=True, source_page=source
        )

    return run_notion_tool(user_id, _do)


def notion_trash_page(
    page_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Archive (soft-delete) a Notion page — restorable from Notion trash UI."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        result = client.trash_page(page_id)
        return {
            "page_id": page_id,
            "archived": True,
            "url": result.get("url") if isinstance(result, dict) else None,
        }

    return run_notion_tool(user_id, _do)


# ===========================================================================
# Blocks
# ===========================================================================


class NotionGetBlockChildrenArgs(BaseModel):
    block_id: str = Field(description="Block or page ID whose children to list.")
    max_results: int = Field(
        default=100, ge=1, le=100, description="Max children to return (1-100)."
    )
    start_cursor: Optional[str] = Field(
        default=None, description="Pagination cursor from a prior call."
    )


class NotionUpdateBlockArgs(BaseModel):
    block_id: str = Field(description="ID of the block to update.")
    block: Dict[str, Any] = Field(
        description="Partial block payload (e.g. {'paragraph': {'rich_text': [...]}}). Passed through to PATCH /blocks/{id}."
    )


class NotionDeleteBlockArgs(BaseModel):
    block_id: str = Field(description="ID of the block to delete (archive).")


def notion_get_block_children(
    block_id: str,
    max_results: int = 100,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List child blocks under a block or page."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        page = client.get_block_children(
            block_id, start_cursor=start_cursor, page_size=max_results
        )
        return {
            "block_id": block_id,
            "count": len(page.get("results") or []),
            "results": page.get("results") or [],
            "has_more": page.get("has_more", False),
            "next_cursor": page.get("next_cursor"),
        }

    return run_notion_tool(user_id, _do)


def notion_update_block(
    block_id: str,
    block: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update a block's content. Non-destructive but overwrites rich_text."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        result = client.update_block(block_id, block)
        return {
            "block_id": block_id,
            "type": result.get("type") if isinstance(result, dict) else None,
            "updated": True,
        }

    return run_notion_tool(user_id, _do)


def notion_delete_block(
    block_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Delete (archive) a block. Safety: irreversible from API — use carefully."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        client.delete_block(block_id)
        return {"block_id": block_id, "deleted": True}

    return run_notion_tool(user_id, _do)
