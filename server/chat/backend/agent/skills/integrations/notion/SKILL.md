---
name: notion
id: notion
description: "Notion workspace integration for searching pages, managing databases, creating postmortems, and exporting RCA findings"
category: knowledge
connection_check:
  method: get_token_data
  provider_key: notion
  required_any_fields: [access_token, token]
tools:
  - notion_search
  - notion_fetch
  - notion_create_pages
  - notion_update_page
  - notion_append_to_page
  - notion_move_pages
  - notion_duplicate_page
  - notion_trash_page
  - notion_get_block_children
  - notion_update_block
  - notion_delete_block
  - notion_create_database
  - notion_update_database
  - notion_update_database_properties
  - notion_query_database
  - notion_create_data_source
  - notion_get_data_source
  - notion_update_data_source
  - notion_update_data_source_properties
  - notion_query_data_source
  - notion_list_data_source_templates
  - notion_create_view
  - notion_update_view
  - notion_delete_view
  - notion_list_database_views
  - notion_query_view
  - notion_create_comment
  - notion_get_comments
  - notion_list_users
  - notion_get_user
  - notion_get_self
  - notion_find_person
  - notion_list_teamspaces
  - notion_upload_file
  - notion_list_file_uploads
  - notion_list_custom_emojis
  - notion_export_postmortem
  - notion_create_action_items
index: "Knowledge workspace -- search pages, manage databases, export postmortems & action items"
rca_priority: 4
allowed-tools: notion_search, notion_fetch, notion_create_pages, notion_update_page, notion_append_to_page, notion_move_pages, notion_duplicate_page, notion_trash_page, notion_get_block_children, notion_update_block, notion_delete_block, notion_create_database, notion_update_database, notion_update_database_properties, notion_query_database, notion_create_data_source, notion_get_data_source, notion_update_data_source, notion_update_data_source_properties, notion_query_data_source, notion_list_data_source_templates, notion_create_view, notion_update_view, notion_delete_view, notion_list_database_views, notion_query_view, notion_create_comment, notion_get_comments, notion_list_users, notion_get_user, notion_get_self, notion_find_person, notion_list_teamspaces, notion_upload_file, notion_list_file_uploads, notion_list_custom_emojis, notion_export_postmortem, notion_create_action_items
metadata:
  author: aurora
  version: "1.0"
---

# Notion Integration

## Overview
Notion workspace integration for searching, reading, and managing pages, databases, blocks, comments, users, and files. Also supports exporting Aurora RCA postmortems and action items to Notion databases.

Notion is a REMOTE service. Use ONLY the Notion tools listed below -- never shell commands or local file access.

## Instructions

### 1. Discovery -- Always Start Here
Before any operation, search to understand the workspace layout:

`notion_search(query='...', types=['page'])` -- find pages by title keyword
`notion_search(query='...', types=['database'])` -- find databases by title keyword
`notion_search(query='...', max_results=25)` -- search all object types (pages + databases)

- `types` accepts `["page"]`, `["database"]`, or omit for both.
- `max_results` range: 1--100, default 10.
- Returns: id, object type, title, url, last_edited_time for each result.

To read a specific page or database:
`notion_fetch(url_or_id='<page_url_or_uuid>', max_length=5000)` -- returns page body as markdown
- Accepts full Notion URLs or bare UUIDs (with or without dashes).
- `max_length` range: 100--50000, default 5000. Output truncated with "... [truncated]" marker beyond this.
- For databases: returns property schema keys instead of markdown body.
- For long pages, use `notion_get_block_children` with pagination instead.

### 2. Pages

**Create pages:**
`notion_create_pages(pages=[{title, parent_page_id|parent_database_id, markdown, icon, cover, properties}])`
- Each page needs exactly ONE parent: `parent_page_id` (under a page) or `parent_database_id` (as a database row).
- `markdown`: optional body content as markdown string.
- `icon`: emoji string (e.g. "📋") or image URL.
- `cover`: image URL.
- `properties`: raw Notion property payload (for database-parented pages with custom schema columns).
- Batch-creates multiple pages in one call; returns per-page results + errors for partial failures.

**Update a page:**
`notion_update_page(page_id, properties=None, icon=None, cover=None, archived=None, markdown=None, markdown_mode='append')`
- `markdown_mode`: `"append"` (default, adds to end) or `"replace"` (overwrites body).
- Only provided fields are updated; omitted fields are unchanged.

**Append content:**
`notion_append_to_page(page_id, markdown)` -- append markdown to the end of a page.

**Move pages:**
`notion_move_pages(page_ids=[...], new_parent_id)` -- move one or more pages under a new parent page.
- Returns per-page results + errors for partial failures.

**Duplicate a page:**
`notion_duplicate_page(page_id, new_parent_id=None)` -- deep-copy (title, icon, cover, markdown body).
- If `new_parent_id` omitted, duplicates under the same parent.
- Useful for template instantiation.

**Trash (archive) a page:**
`notion_trash_page(page_id)` -- soft-delete; recoverable from Notion trash UI.

### 3. Blocks

**List child blocks:**
`notion_get_block_children(block_id, max_results=100, start_cursor=None)`
- `max_results` range: 1--100, default 100.
- Returns: results array, `has_more` flag, `next_cursor` for pagination.
- Use `start_cursor` from a previous response to get the next page.

**Update a block:**
`notion_update_block(block_id, block={...})` -- pass partial block payload.
- Example: `{"paragraph": {"rich_text": [{"type": "text", "text": {"content": "new text"}}]}}`
- Overwrites that block's rich_text content.

**Delete a block:**
`notion_delete_block(block_id)` -- permanent deletion from API (irreversible). Use with extreme care; prefer `notion_trash_page` for pages.

### 4. Databases

**Create a database:**
`notion_create_database(parent_page_id, title, properties, icon=None, cover=None)`
- `properties`: Notion property schema object defining columns.
- Supported property types: `title`, `rich_text`, `number`, `select`, `multi_select`, `status`, `date`, `people`, `files`, `checkbox`, `url`, `email`, `phone_number`, `formula`, `relation`, `rollup`, `created_time`, `created_by`, `last_edited_time`, `last_edited_by`.

**Update database metadata:**
`notion_update_database(database_id, title=None, description=None, icon=None, cover=None, archived=None)`
- `description`: plain text (converted to rich_text internally).

**Update database schema:**
`notion_update_database_properties(database_id, properties={...})` -- add, rename, or remove columns.
- **DESTRUCTIVE**: setting a property value to `null` REMOVES that column and all its data. Always confirm with the user before removing columns.

**Query a database:**
`notion_query_database(database_id, filter=None, sorts=None, max_results=25, start_cursor=None)`
- `max_results` range: 1--100, default 25.
- Returns: results with id, url, title, last_edited_time, full properties dict, plus `has_more`/`next_cursor` for pagination.

#### Filter Syntax (Notion API format)
Filters use property name + type + condition:

**Text (title, rich_text, url, email, phone_number):**
```json
{"property": "Name", "title": {"contains": "incident"}}
{"property": "Description", "rich_text": {"is_not_empty": true}}
```

**Number:**
```json
{"property": "Priority", "number": {"greater_than": 3}}
{"property": "Score", "number": {"equals": 100}}
```

**Select:**
```json
{"property": "Status", "select": {"equals": "Done"}}
{"property": "Type", "select": {"does_not_equal": "Bug"}}
```

**Multi-select:**
```json
{"property": "Tags", "multi_select": {"contains": "P0"}}
```

**Checkbox:**
```json
{"property": "Completed", "checkbox": {"equals": true}}
```

**Date:**
```json
{"property": "Due", "date": {"on_or_before": "2025-01-15"}}
{"property": "Created", "date": {"after": "2025-01-01"}}
{"property": "Due", "date": {"past_week": {}}}
{"property": "Due", "date": {"next_month": {}}}
```
Date conditions: `equals`, `before`, `after`, `on_or_before`, `on_or_after`, `is_empty`, `is_not_empty`, `past_week`, `past_month`, `past_year`, `next_week`, `next_month`, `next_year`.

**Timestamp (created_time / last_edited_time):**
```json
{"timestamp": "created_time", "created_time": {"on_or_before": "2025-01-13"}}
```

**Relation:**
```json
{"property": "Tasks", "relation": {"contains": "<page_uuid>"}}
```

**Compound filters (AND / OR):**
```json
{"and": [
  {"property": "Status", "select": {"equals": "Active"}},
  {"property": "Priority", "number": {"greater_than": 2}}
]}
```

#### Sort Syntax
**By property:**
```json
[{"property": "Priority", "direction": "descending"}]
```
**By timestamp:**
```json
[{"timestamp": "last_edited_time", "direction": "descending"}]
```
Multiple sorts are applied in order (first sort is primary).

### 5. Users & People

`notion_list_users(max_results=100, start_cursor=None)` -- list all workspace members (people + bots).
`notion_get_user(target_user_id)` -- fetch a single user by ID.
`notion_get_self()` -- get the Aurora bot user (the integration itself).
`notion_find_person(name_or_email)` -- find a person:
- Email (contains `@`): exact match.
- Name: case-insensitive substring search across up to 20 pages of users.
- Returns: found (bool), count, users array.

`notion_list_teamspaces()` -- best-effort listing of teamspaces (uses workspace search internally). Often empty on Free/Plus plans.

### 6. Comments

**Post a comment:**
`notion_create_comment(text, page_id=None, block_id=None, discussion_id=None)`
- Provide exactly ONE of: `page_id` (new top-level comment), `block_id` (comment on a block), `discussion_id` (reply to existing thread).
- `text`: plain text (converted to rich_text internally). Supports inline formatting only (bold, italic, code, links) -- no block-level markdown.

**Read comments:**
`notion_get_comments(page_id_or_block_id, include_resolved=False)`
- `include_resolved=False` (default): filters out resolved comments.
- Returns: count, comments with id, discussion_id, author_id, text, created_time, resolved flag.

### 7. Files

**Upload a file:**
`notion_upload_file(file_path_or_url, filename=None, content_type=None)`
- Accepts local file path OR public https URL.
- Auto-selects single-part (< 20 MB) or multi-part (10 MB chunks, up to 500 MB).
- Content-type auto-detected if not provided.
- Returns: upload_id (reference this when creating/updating pages), filename, size_bytes, status.
- SSRF protection: URLs resolving to private/loopback IPs are blocked.

**List uploads:**
`notion_list_file_uploads(max_results=25, start_cursor=None)` -- list recent file uploads by this integration.

### 8. Custom Emojis

`notion_list_custom_emojis()` -- list custom emojis in the workspace. Returns `supported: false` on plans without this feature.

### 9. Data Sources (Plan-Gated)

These endpoints require specific Notion plans. All return `{"supported": false}` gracefully on workspaces without access.

`notion_create_data_source(payload)` -- create a data source (full payload passed through).
`notion_get_data_source(data_source_id)` -- fetch by ID.
`notion_update_data_source(data_source_id, updates)` -- update attributes.
`notion_update_data_source_properties(data_source_id, properties)` -- update schema.
`notion_query_data_source(data_source_id, filter=None, sorts=None, max_results=25, start_cursor=None)` -- query with filter/sort/pagination.
`notion_list_data_source_templates(data_source_id)` -- list templates.

### 10. Database Views (Plan-Gated)

These endpoints require specific Notion plans. All return `{"supported": false}` gracefully on workspaces without access.

`notion_create_view(payload)` -- create a view on a database.
`notion_update_view(view_id, updates)` -- update filters, sorts, visible properties.
`notion_delete_view(view_id)` -- **irreversible** deletion of a view.
`notion_list_database_views(database_id)` -- list all views on a database.
`notion_query_view(view_id, filter=None, sorts=None, max_results=25, start_cursor=None)` -- query through a view.

### 11. RCA Postmortem Export (Aurora-Specific)

**Export a postmortem:**
`notion_export_postmortem(incident_id, database_id, action_items_database_id=None)`
- `incident_id`: UUID of the Aurora incident.
- `database_id`: target Notion database for the postmortem page.
- `action_items_database_id`: optional separate database for action items.
- Creates a structured page with title "Postmortem -- Incident {id}", timeline, root cause, impact sections.
- Auto-maps incident properties to database columns:
  - "IncidentId" -> id, "Severity" -> severity, "Status" -> status, "Service" -> alert_service, "ResolvedAt" -> resolved_at.
- Supports property types: date, rich_text, title, select, multi_select, number, checkbox, url, email, phone_number.
- Updates the Aurora postmortem record with the Notion page URL after export.

**Create action items:**
`notion_create_action_items(incident_id, action_items_database_id, assignee_hints=None)`
- Parses unchecked markdown checkboxes from the postmortem body.
- Creates one Notion database row per action item.
- `assignee_hints`: optional map `{"item_text_prefix": "user@email.com"}` to assign owners.
- Auto-detects title, people, and date properties in the target database schema.
- Assignees resolved by email lookup against workspace users.

## RCA Investigation Workflow

**IMPORTANT: You MUST search Notion early in every RCA investigation.** The workspace contains past postmortems, runbooks, and action items that provide critical historical context. Do NOT skip this step.

**IMPORTANT: During RCA, Notion is READ-ONLY.** Do NOT create pages, export postmortems, create action items, or write to Notion. The engineer reviews your findings and exports via the UI after approving.

**Step 1 (EARLY -- do this within your first 3 tool calls) -- Search Notion for historical context:**
`notion_search(query='<service_name>', types=['page'])` to find past postmortems or runbooks related to the affected service.
Also try: `notion_search(query='<alert_type_or_keyword>')` for broader matches.

**Step 2 -- Query postmortem databases for patterns:**
`notion_search(query='postmortem', types=['database'])` to find postmortem databases, then:
`notion_query_database(database_id='...', filter={"property": "Service", "select": {"equals": "<service_name>"}}, sorts=[{"timestamp": "created_time", "direction": "descending"}])`

**Step 3 -- Read relevant past postmortems:**
`notion_fetch(url_or_id='<page_id>', max_length=10000)` to get full postmortem content. Past incidents on the same service often reveal recurring root causes.

## Important Rules
- Notion is a REMOTE service. Use ONLY the Notion tools listed above -- never local commands.
- All `url_or_id` / `page_id` / `database_id` parameters accept UUIDs with or without dashes.
- `notion_fetch` truncates at `max_length` characters. For full content of long pages, paginate with `notion_get_block_children`.
- Write operations (create, update, append, move, trash, delete) modify the user's live Notion workspace. Confirm destructive actions before executing.
- `notion_update_database_properties` with a null value REMOVES a column and its data. Always confirm.
- `notion_delete_block` is permanent and irreversible from the API.
- `notion_delete_view` is irreversible.
- Rich text content is capped at ~2000 characters per segment by Notion.
- Pagination: all list/query endpoints return `has_more` (bool) and `next_cursor` (string). Pass `start_cursor=next_cursor` to fetch the next page.
- Data sources, views, and custom emojis are plan-gated features. Tools return `{"supported": false}` on workspaces without access -- do not retry, inform the user.
- Rate limiting: the Notion API enforces 3 requests/second. The client handles retries automatically (429 with Retry-After, 5xx with backoff). No manual throttling needed.
- Auth errors: if a tool returns `code: "reauth_required"`, ask the user to reconnect Notion at the connection page, then retry.
- If a tool returns `code: "not_connected"`, ask the user to connect Notion first.
