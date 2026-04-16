"""Notion structured-data chat tools: databases, data sources, views."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .common import (
    build_cover,
    build_icon,
    build_rich_text,
    extract_title,
    run_notion_tool,
    wrap_optional_feature,
)


def _ds_pass(raw: Any) -> Dict[str, Any]:
    return wrap_optional_feature(raw, "data_sources_not_available")


def _view_pass(raw: Any) -> Dict[str, Any]:
    return wrap_optional_feature(raw, "views_not_available")


# ===========================================================================
# Databases
# ===========================================================================


class NotionCreateDatabaseArgs(BaseModel):
    parent_page_id: str = Field(
        description="Parent page ID under which the new database is created."
    )
    title: str = Field(description="Database title.")
    properties: Dict[str, Any] = Field(
        description="Notion properties schema, e.g. {'Name': {'title': {}}, 'Status': {'select': {...}}}."
    )
    icon: Optional[str] = Field(
        default=None, description="Optional emoji or image URL for the icon."
    )
    cover: Optional[str] = Field(
        default=None, description="Optional cover image URL."
    )


class NotionUpdateDatabaseArgs(BaseModel):
    database_id: str = Field(description="ID of the database to update.")
    title: Optional[str] = Field(
        default=None, description="New title (plain text)."
    )
    description: Optional[str] = Field(
        default=None, description="New description (plain text)."
    )
    icon: Optional[str] = Field(
        default=None, description="Emoji or image URL for the icon."
    )
    cover: Optional[str] = Field(
        default=None, description="Image URL for the cover."
    )
    archived: Optional[bool] = Field(
        default=None, description="Archive/restore the database."
    )


class NotionUpdateDatabasePropertiesArgs(BaseModel):
    database_id: str = Field(description="ID of the database whose schema to update.")
    properties: Dict[str, Any] = Field(
        description="Partial properties schema. Pass a property value of null to remove a column."
    )


class NotionQueryDatabaseArgs(BaseModel):
    database_id: str = Field(description="ID of the database to query.")
    filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Notion filter object (optional)."
    )
    sorts: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Notion sorts array (optional)."
    )
    max_results: int = Field(
        default=25, ge=1, le=100, description="Page size (1-100)."
    )
    start_cursor: Optional[str] = Field(
        default=None, description="Pagination cursor from a prior query."
    )


def notion_create_database(
    parent_page_id: str,
    title: str,
    properties: Dict[str, Any],
    icon: Optional[str] = None,
    cover: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a Notion database under a parent page."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        db = client.create_database(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=build_rich_text(title or "Untitled"),
            properties=properties,
            icon=build_icon(icon),
            cover=build_cover(cover),
        )
        return {
            "id": db.get("id"),
            "url": db.get("url"),
            "title": extract_title(db) or title,
            "properties": list((db.get("properties") or {}).keys()),
        }

    return run_notion_tool(user_id, _do)


def notion_update_database(
    database_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    cover: Optional[str] = None,
    archived: Optional[bool] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update database attributes (title, description, icon, cover, archived)."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        updates: Dict[str, Any] = {}
        if title is not None:
            updates["title"] = build_rich_text(title)
        if description is not None:
            updates["description"] = build_rich_text(description)
        icon_payload = build_icon(icon)
        if icon_payload is not None:
            updates["icon"] = icon_payload
        cover_payload = build_cover(cover)
        if cover_payload is not None:
            updates["cover"] = cover_payload
        if archived is not None:
            updates["archived"] = archived

        result = client.update_database(database_id, **updates)
        return {
            "id": database_id,
            "url": result.get("url") if isinstance(result, dict) else None,
            "updated_fields": list(updates.keys()),
        }

    return run_notion_tool(user_id, _do)


def notion_update_database_properties(
    database_id: str,
    properties: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update a database's schema (add, rename, change, or remove columns)."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        result = client.update_database_properties(database_id, properties)
        return {
            "id": database_id,
            "properties": list((result.get("properties") or {}).keys())
            if isinstance(result, dict)
            else [],
            "updated": True,
        }

    return run_notion_tool(user_id, _do)


def notion_query_database(
    database_id: str,
    filter: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
    max_results: int = 25,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Query a Notion database with optional filter/sorts and pagination."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        raw = client.query_database(
            database_id,
            filter=filter,
            sorts=sorts,
            max_results=max_results,
            start_cursor=start_cursor,
        )
        results = [
            {
                "id": item.get("id"),
                "url": item.get("url"),
                "title": extract_title(item),
                "last_edited_time": item.get("last_edited_time"),
                "properties": item.get("properties") or {},
            }
            for item in raw.get("results") or []
        ]
        return {
            "database_id": database_id,
            "count": len(results),
            "results": results,
            "has_more": raw.get("has_more", False),
            "next_cursor": raw.get("next_cursor"),
        }

    return run_notion_tool(user_id, _do)


# ===========================================================================
# Data sources (newer API; may 404 on older workspaces)
# ===========================================================================


class NotionCreateDataSourceArgs(BaseModel):
    payload: Dict[str, Any] = Field(
        description="Full create-data-source payload (passed through to POST /data_sources)."
    )


class NotionGetDataSourceArgs(BaseModel):
    data_source_id: str = Field(description="ID of the data source to fetch.")


class NotionUpdateDataSourceArgs(BaseModel):
    data_source_id: str = Field(description="ID of the data source to update.")
    updates: Dict[str, Any] = Field(
        description="Partial updates payload (passed through to PATCH /data_sources/{id})."
    )


class NotionUpdateDataSourcePropertiesArgs(BaseModel):
    data_source_id: str = Field(description="ID of the data source.")
    properties: Dict[str, Any] = Field(
        description="Properties schema patch."
    )


class NotionQueryDataSourceArgs(BaseModel):
    data_source_id: str = Field(description="Data source ID to query.")
    filter: Optional[Dict[str, Any]] = Field(default=None, description="Notion filter object.")
    sorts: Optional[List[Dict[str, Any]]] = Field(default=None, description="Notion sorts array.")
    max_results: int = Field(default=25, ge=1, le=100, description="Page size (1-100).")
    start_cursor: Optional[str] = Field(default=None, description="Pagination cursor.")


class NotionListDataSourceTemplatesArgs(BaseModel):
    data_source_id: str = Field(description="Data source ID whose templates to list.")


def notion_create_data_source(
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a Notion data source. Returns supported=False on workspaces without data-source support."""
    _ = session_id
    return run_notion_tool(user_id, lambda c: _ds_pass(c.create_data_source(payload)))


def notion_get_data_source(
    data_source_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a Notion data source by ID."""
    _ = session_id
    return run_notion_tool(
        user_id, lambda c: _ds_pass(c.get_data_source(data_source_id))
    )


def notion_update_data_source(
    data_source_id: str,
    updates: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update a Notion data source's attributes."""
    _ = session_id
    return run_notion_tool(
        user_id,
        lambda c: _ds_pass(c.update_data_source(data_source_id, **updates)),
    )


def notion_update_data_source_properties(
    data_source_id: str,
    properties: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update a Notion data source's properties schema."""
    _ = session_id
    return run_notion_tool(
        user_id,
        lambda c: _ds_pass(
            c.update_data_source_properties(data_source_id, properties)
        ),
    )


def notion_query_data_source(
    data_source_id: str,
    filter: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
    max_results: int = 25,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Query a Notion data source with optional filter/sorts/pagination."""
    _ = session_id
    return run_notion_tool(
        user_id,
        lambda c: _ds_pass(
            c.query_data_source(
                data_source_id,
                filter=filter,
                sorts=sorts,
                max_results=max_results,
                start_cursor=start_cursor,
            )
        ),
    )


def notion_list_data_source_templates(
    data_source_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List templates attached to a Notion data source."""
    _ = session_id
    return run_notion_tool(
        user_id,
        lambda c: _ds_pass(c.list_data_source_templates(data_source_id)),
    )


# ===========================================================================
# Views (newer API; may 404 on older workspaces)
# ===========================================================================


class NotionCreateViewArgs(BaseModel):
    payload: Dict[str, Any] = Field(
        description="Full create-view payload (passed through to POST /views)."
    )


class NotionUpdateViewArgs(BaseModel):
    view_id: str = Field(description="ID of the view to update.")
    updates: Dict[str, Any] = Field(
        description="Partial updates payload (passed through to PATCH /views/{id})."
    )


class NotionDeleteViewArgs(BaseModel):
    view_id: str = Field(description="ID of the view to delete. Safety: removes the view from the database.")


class NotionListDatabaseViewsArgs(BaseModel):
    database_id: str = Field(description="Database ID whose views to list.")


class NotionQueryViewArgs(BaseModel):
    view_id: str = Field(description="View ID to query.")
    filter: Optional[Dict[str, Any]] = Field(default=None, description="Notion filter object (optional).")
    sorts: Optional[List[Dict[str, Any]]] = Field(default=None, description="Notion sorts array (optional).")
    max_results: int = Field(default=25, ge=1, le=100, description="Page size (1-100).")
    start_cursor: Optional[str] = Field(default=None, description="Pagination cursor.")


def notion_create_view(
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a Notion database view."""
    _ = session_id
    return run_notion_tool(user_id, lambda c: _view_pass(c.create_view(payload)))


def notion_update_view(
    view_id: str,
    updates: Dict[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update a Notion view (filters, sorts, displayed properties)."""
    _ = session_id
    return run_notion_tool(
        user_id, lambda c: _view_pass(c.update_view(view_id, **updates))
    )


def notion_delete_view(
    view_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Delete a Notion database view. Safety: irreversible."""
    _ = session_id
    return run_notion_tool(user_id, lambda c: _view_pass(c.delete_view(view_id)))


def notion_list_database_views(
    database_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List all views attached to a Notion database."""
    _ = session_id
    return run_notion_tool(
        user_id, lambda c: _view_pass(c.list_database_views(database_id))
    )


def notion_query_view(
    view_id: str,
    filter: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
    max_results: int = 25,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Query a Notion view with optional filter/sorts/pagination."""
    _ = session_id
    return run_notion_tool(
        user_id,
        lambda c: _view_pass(
            c.query_view(
                view_id,
                filter=filter,
                sorts=sorts,
                max_results=max_results,
                start_cursor=start_cursor,
            )
        ),
    )
