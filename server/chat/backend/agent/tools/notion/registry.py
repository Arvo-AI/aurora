"""Tool registry: ``NOTION_TOOL_SPECS`` consumed by ``cloud_tools.get_cloud_tools``."""

from __future__ import annotations

from .content import (
    NotionAppendToPageArgs,
    NotionCreatePagesArgs,
    NotionDeleteBlockArgs,
    NotionDuplicatePageArgs,
    NotionFetchArgs,
    NotionGetBlockChildrenArgs,
    NotionMovePagesArgs,
    NotionSearchArgs,
    NotionTrashPageArgs,
    NotionUpdateBlockArgs,
    NotionUpdatePageArgs,
    notion_append_to_page,
    notion_create_pages,
    notion_delete_block,
    notion_duplicate_page,
    notion_fetch,
    notion_get_block_children,
    notion_move_pages,
    notion_search,
    notion_trash_page,
    notion_update_block,
    notion_update_page,
)
from .postmortem import (
    NotionCreateActionItemsArgs,
    NotionExportPostmortemArgs,
    notion_create_action_items,
    notion_export_postmortem,
)
from .structured import (
    NotionCreateDatabaseArgs,
    NotionCreateDataSourceArgs,
    NotionCreateViewArgs,
    NotionDeleteViewArgs,
    NotionGetDataSourceArgs,
    NotionListDatabaseViewsArgs,
    NotionListDataSourceTemplatesArgs,
    NotionQueryDatabaseArgs,
    NotionQueryDataSourceArgs,
    NotionQueryViewArgs,
    NotionUpdateDatabaseArgs,
    NotionUpdateDatabasePropertiesArgs,
    NotionUpdateDataSourceArgs,
    NotionUpdateDataSourcePropertiesArgs,
    NotionUpdateViewArgs,
    notion_create_database,
    notion_create_data_source,
    notion_create_view,
    notion_delete_view,
    notion_get_data_source,
    notion_list_data_source_templates,
    notion_list_database_views,
    notion_query_database,
    notion_query_data_source,
    notion_query_view,
    notion_update_database,
    notion_update_database_properties,
    notion_update_data_source,
    notion_update_data_source_properties,
    notion_update_view,
)
from .workspace import (
    NotionCreateCommentArgs,
    NotionFindPersonArgs,
    NotionGetCommentsArgs,
    NotionGetSelfArgs,
    NotionGetUserArgs,
    NotionListCustomEmojisArgs,
    NotionListFileUploadsArgs,
    NotionListTeamspacesArgs,
    NotionListUsersArgs,
    NotionUploadFileArgs,
    notion_create_comment,
    notion_find_person,
    notion_get_comments,
    notion_get_self,
    notion_get_user,
    notion_list_custom_emojis,
    notion_list_file_uploads,
    notion_list_teamspaces,
    notion_list_users,
    notion_upload_file,
)


def _spec(func, name, schema, desc):
    return (func, name, schema, desc)


NOTION_TOOL_SPECS = [
    # Search / fetch
    _spec(
        notion_search,
        "notion_search",
        NotionSearchArgs,
        "Search the connected Notion workspace for pages and/or databases. Live, ACL-aware. "
        "Returns titles, URLs, and last-edited timestamps. Read-only.",
    ),
    _spec(
        notion_fetch,
        "notion_fetch",
        NotionFetchArgs,
        "Fetch a Notion page, database, or block by URL or ID. Returns page body as markdown (truncated). Read-only.",
    ),
    # Pages
    _spec(
        notion_create_pages,
        "notion_create_pages",
        NotionCreatePagesArgs,
        "Create one or more Notion pages under a parent page or database. Page body is supplied as markdown. "
        "Side effect: writes to Notion.",
    ),
    _spec(
        notion_update_page,
        "notion_update_page",
        NotionUpdatePageArgs,
        "Update a Notion page's properties, icon, cover, archived flag, and/or append/replace its markdown body. "
        "Side effect: writes to Notion.",
    ),
    _spec(
        notion_append_to_page,
        "notion_append_to_page",
        NotionAppendToPageArgs,
        "Append markdown content to the end of an existing Notion page. Non-destructive write.",
    ),
    _spec(
        notion_move_pages,
        "notion_move_pages",
        NotionMovePagesArgs,
        "Move one or more Notion pages under a new parent page. Non-destructive write.",
    ),
    _spec(
        notion_duplicate_page,
        "notion_duplicate_page",
        NotionDuplicatePageArgs,
        "Duplicate a Notion page (title, icon, cover, markdown body) under the same or a new parent. "
        "Useful for template instantiation. Non-destructive write.",
    ),
    _spec(
        notion_trash_page,
        "notion_trash_page",
        NotionTrashPageArgs,
        "Archive (soft-delete / trash) a Notion page. Safety: recoverable from Notion trash UI.",
    ),
    # Blocks
    _spec(
        notion_get_block_children,
        "notion_get_block_children",
        NotionGetBlockChildrenArgs,
        "List child blocks under a Notion block or page. Read-only. Supports pagination via start_cursor.",
    ),
    _spec(
        notion_update_block,
        "notion_update_block",
        NotionUpdateBlockArgs,
        "Update a single Notion block's content by passing the partial block payload. "
        "Side effect: overwrites that block's content.",
    ),
    _spec(
        notion_delete_block,
        "notion_delete_block",
        NotionDeleteBlockArgs,
        "Delete (archive) a Notion block. Safety: this affects only one block; use with care.",
    ),
    # Databases
    _spec(
        notion_create_database,
        "notion_create_database",
        NotionCreateDatabaseArgs,
        "Create a Notion database under a parent page with a properties schema. Side effect: writes to Notion.",
    ),
    _spec(
        notion_update_database,
        "notion_update_database",
        NotionUpdateDatabaseArgs,
        "Update a Notion database's title, description, icon, cover, or archived flag. Side effect: writes to Notion.",
    ),
    _spec(
        notion_update_database_properties,
        "notion_update_database_properties",
        NotionUpdateDatabasePropertiesArgs,
        "Update a Notion database's properties schema (add/rename/remove columns). "
        "Safety: passing a property value of null removes that column — destructive for that column's data.",
    ),
    _spec(
        notion_query_database,
        "notion_query_database",
        NotionQueryDatabaseArgs,
        "Query a Notion database with optional filter + sorts. Read-only. Supports pagination.",
    ),
    # Data sources
    _spec(
        notion_create_data_source,
        "notion_create_data_source",
        NotionCreateDataSourceArgs,
        "Create a Notion data source. Returns supported=False on workspaces without data-source API. Side effect: writes to Notion.",
    ),
    _spec(
        notion_get_data_source,
        "notion_get_data_source",
        NotionGetDataSourceArgs,
        "Fetch a Notion data source by ID. Read-only. Returns supported=False on older workspaces.",
    ),
    _spec(
        notion_update_data_source,
        "notion_update_data_source",
        NotionUpdateDataSourceArgs,
        "Update a Notion data source's attributes. Side effect: writes to Notion.",
    ),
    _spec(
        notion_update_data_source_properties,
        "notion_update_data_source_properties",
        NotionUpdateDataSourcePropertiesArgs,
        "Update a Notion data source's properties schema. Side effect: writes to Notion.",
    ),
    _spec(
        notion_query_data_source,
        "notion_query_data_source",
        NotionQueryDataSourceArgs,
        "Query a Notion data source with optional filter/sorts/pagination. Read-only.",
    ),
    _spec(
        notion_list_data_source_templates,
        "notion_list_data_source_templates",
        NotionListDataSourceTemplatesArgs,
        "List templates attached to a Notion data source. Read-only.",
    ),
    # Views
    _spec(
        notion_create_view,
        "notion_create_view",
        NotionCreateViewArgs,
        "Create a new view on a Notion database. Side effect: writes to Notion. Returns supported=False where views API is unavailable.",
    ),
    _spec(
        notion_update_view,
        "notion_update_view",
        NotionUpdateViewArgs,
        "Update a Notion database view's filters, sorts, or visible properties. Side effect: writes to Notion.",
    ),
    _spec(
        notion_delete_view,
        "notion_delete_view",
        NotionDeleteViewArgs,
        "Delete a Notion database view. Safety: irreversible.",
    ),
    _spec(
        notion_list_database_views,
        "notion_list_database_views",
        NotionListDatabaseViewsArgs,
        "List all views attached to a Notion database. Read-only.",
    ),
    _spec(
        notion_query_view,
        "notion_query_view",
        NotionQueryViewArgs,
        "Query a Notion database view with optional filter/sorts. Read-only.",
    ),
    # Comments
    _spec(
        notion_create_comment,
        "notion_create_comment",
        NotionCreateCommentArgs,
        "Post a comment on a Notion page, block, or discussion thread. Non-destructive write.",
    ),
    _spec(
        notion_get_comments,
        "notion_get_comments",
        NotionGetCommentsArgs,
        "List comments attached to a Notion page or block. Read-only.",
    ),
    # Users
    _spec(
        notion_list_users,
        "notion_list_users",
        NotionListUsersArgs,
        "List members (people + bots) of the connected Notion workspace. Read-only.",
    ),
    _spec(
        notion_get_user,
        "notion_get_user",
        NotionGetUserArgs,
        "Fetch a single Notion user by ID. Read-only.",
    ),
    _spec(
        notion_get_self,
        "notion_get_self",
        NotionGetSelfArgs,
        "Get the Notion bot user associated with the current integration. Read-only.",
    ),
    _spec(
        notion_find_person,
        "notion_find_person",
        NotionFindPersonArgs,
        "Find a Notion person by email (exact) or name (case-insensitive substring). Read-only.",
    ),
    _spec(
        notion_list_teamspaces,
        "notion_list_teamspaces",
        NotionListTeamspacesArgs,
        "Best-effort listing of teamspaces (via workspace search). Empty on Free/Plus plans. Read-only.",
    ),
    # Files
    _spec(
        notion_upload_file,
        "notion_upload_file",
        NotionUploadFileArgs,
        "Upload a file (local path or https URL) to Notion via the file_uploads API. "
        "Automatically chooses single-part (<20MB) or multi-part (10MB chunks). Capped at 500MB. "
        "Returns the Notion file_upload ID that can be referenced when creating/updating pages.",
    ),
    _spec(
        notion_list_file_uploads,
        "notion_list_file_uploads",
        NotionListFileUploadsArgs,
        "List recent Notion file uploads created by this integration. Read-only.",
    ),
    # Emojis
    _spec(
        notion_list_custom_emojis,
        "notion_list_custom_emojis",
        NotionListCustomEmojisArgs,
        "List custom emojis defined in the connected Notion workspace. Read-only.",
    ),
    # Aurora-specific RCA export
    _spec(
        notion_export_postmortem,
        "notion_export_postmortem",
        NotionExportPostmortemArgs,
        "Export an Aurora incident postmortem to a Notion database page "
        "(plus optional action-items DB rows). Side effect: writes to Notion.",
    ),
    _spec(
        notion_create_action_items,
        "notion_create_action_items",
        NotionCreateActionItemsArgs,
        "Create Notion database rows for each unchecked action item in the "
        "postmortem's Action Items section. Side effect: writes to Notion.",
    ),
]
