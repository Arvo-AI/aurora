---
name: sharepoint
id: sharepoint
description: "SharePoint integration for searching documents, pages, and publishing incident reports via Microsoft Graph API"
category: knowledge
connection_check:
  method: get_token_data
  provider_key: sharepoint
  required_field: access_token
  feature_flag: is_sharepoint_enabled
tools:
  - sharepoint_search
  - sharepoint_fetch_page
  - sharepoint_fetch_document
  - sharepoint_create_page
index: "Knowledge -- search SharePoint documents and pages, fetch content, publish reports"
rca_priority: 1
allowed-tools: sharepoint_search, sharepoint_fetch_page, sharepoint_fetch_document, sharepoint_create_page
metadata:
  author: aurora
  version: "1.0"
---

# SharePoint Integration

## Overview
SharePoint integration for searching documents and pages via Microsoft Graph API during Root Cause Analysis. Use to find runbooks, architecture docs, and past incident reports stored in SharePoint.

## Instructions

### Tools

- `sharepoint_search(query='error keywords', site_id='optional-site-id')` -- Search across SharePoint for pages, documents, and list items matching a query. Pass a search query and optional `site_id` to restrict to a specific site. Returns matching items with excerpts.
- `sharepoint_fetch_page(site_id='site-id', page_id='page-id')` -- Fetch a SharePoint page by site ID and page ID and return its content as markdown. Use after search to read full page details.
- `sharepoint_fetch_document(drive_id='drive-id', item_id='item-id')` -- Fetch a SharePoint document by drive ID and item ID and return extracted text content. Use for Word docs, PDFs, and other documents stored in SharePoint document libraries.
- `sharepoint_create_page(site_id='site-id', title='...', content='...')` -- Create a new SharePoint page with the given title and HTML/markdown content. Use to publish incident reports, postmortems, or runbooks to SharePoint.

### RCA Investigation Flow

#### Step 1 -- Search for relevant documents
`sharepoint_search(query='error keywords', site_id='optional-site-id')` -- Search across SharePoint for pages, documents, and list items

#### Step 2 -- Read page content
`sharepoint_fetch_page(site_id='site-id', page_id='page-id')` -- Read full page content as markdown

#### Step 3 -- Extract document content
`sharepoint_fetch_document(drive_id='drive-id', item_id='item-id')` -- Extract text from Word docs, PDFs, etc.

### Workflow
Search first to find relevant documents and pages, then fetch content for detailed review.

### Post-Investigation
Use `sharepoint_create_page` to publish incident reports, postmortems, or runbooks back to SharePoint for the team.

### Important Rules
- Search SharePoint early in the investigation for existing runbooks and procedures.
- Use `sharepoint_fetch_page` for web pages and `sharepoint_fetch_document` for uploaded files (Word, PDF).
- Cross-reference findings with live infrastructure state.
