---
name: confluence
id: confluence
description: "Confluence integration for searching runbooks, past incidents, postmortems, and operational procedures during RCA"
category: knowledge
connection_check:
  method: get_token_data
  provider_key: confluence
  required_any_fields:
    - access_token
    - pat_token
  feature_flag: is_confluence_enabled
tools:
  - confluence_search_similar
  - confluence_search_runbooks
  - confluence_fetch_page
  - confluence_runbook_parse
index: "Knowledge -- search Confluence for runbooks, postmortems, past incidents, SOPs"
rca_priority: 1
allowed-tools: confluence_search_similar, confluence_search_runbooks, confluence_fetch_page, confluence_runbook_parse
metadata:
  author: aurora
  version: "1.0"
---

# Confluence Integration

## Overview
Confluence integration for searching runbooks, past incidents, and operational procedures during Root Cause Analysis. Confluence is a **mandatory first step** in any RCA investigation -- search here BEFORE infrastructure or CI/CD tools.

A runbook may give you the exact diagnostic steps. A past postmortem may reveal this is a recurring issue with a known fix.

## Instructions

### MANDATORY FIRST STEP -- RUNBOOKS & PAST INCIDENTS

**You MUST call Confluence tools BEFORE any infrastructure or CI/CD investigation.**
Search Confluence for runbooks and prior postmortems BEFORE deep-diving into infrastructure.

### Tools

- `confluence_search_similar(keywords=['error keywords'], service_name='SERVICE')` -- Search Confluence for pages related to an incident (postmortems, RCA docs). Pass keywords, optional `service_name` and `error_message`. Returns matching pages with excerpts.
- `confluence_search_runbooks(service_name='SERVICE')` -- Search Confluence for runbooks / playbooks / SOPs for a given service. Pass `service_name` and optional `operation` (e.g. 'restart', 'failover').
- `confluence_fetch_page(page_id='12345')` -- Fetch a Confluence page by ID and return its content as markdown. Use after search to read full page details.
- `confluence_runbook_parse(page_url='https://...')` -- Fetch and parse a Confluence runbook into markdown and steps for LLM use.

### RCA Investigation Flow

#### Step 1 -- Search for past incidents with similar symptoms
`confluence_search_similar(keywords=['error keywords'], service_name='SERVICE')` -- Find postmortems / past incidents

#### Step 2 -- Search for runbooks and SOPs
`confluence_search_runbooks(service_name='SERVICE')` -- Find runbooks / SOPs / playbooks

#### Step 3 -- Read full page content for promising results
`confluence_fetch_page(page_id='ID')` -- Read full page content as markdown

#### Step 4 -- Parse runbooks into actionable steps
`confluence_runbook_parse(page_url='URL')` -- Parse a runbook into structured steps

### Workflow
Search first, then fetch promising pages for detailed procedures. Cross-reference Confluence findings with live infrastructure state.

### Important Rules
- Always search Confluence BEFORE deep-diving into infrastructure investigation.
- If a runbook exists for the issue, FOLLOW the documented steps.
- Cross-reference findings with live infrastructure state.
- Past postmortems may reveal this is a recurring issue with a known fix.

## RCA Investigation (Mandatory First Step)
Search Confluence BEFORE deep-diving into infrastructure:
- `confluence_search_similar(keywords=['error keywords'], service_name='{service_name}')` -- Past incidents
- `confluence_search_runbooks(service_name='{service_name}')` -- Runbooks/SOPs
- `confluence_fetch_page(page_id='ID')` -- Full page content

A runbook may give exact diagnostic steps. A past postmortem may reveal a recurring issue.
