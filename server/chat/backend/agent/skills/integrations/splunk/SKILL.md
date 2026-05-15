---
name: splunk
id: splunk
description: "Splunk log analytics integration for searching logs, discovering indexes, and querying sourcetypes via SPL during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.splunk_tool
  function: is_splunk_connected
tools:
  - search_splunk
  - list_splunk_indexes
  - list_splunk_sourcetypes
index: "Log analytics -- discover indexes, list sourcetypes, search logs via SPL"
rca_priority: 3
allowed-tools: search_splunk, list_splunk_indexes, list_splunk_sourcetypes
metadata:
  author: aurora
  version: "1.0"
---

# Splunk Integration

## Overview
Splunk integration for querying log data during Root Cause Analysis. Splunk is a REMOTE service -- do NOT search the local filesystem for Splunk files. Use ONLY the Splunk API tools listed below.

## Instructions

### Tool Usage (use in this order)
1. `list_splunk_indexes()` -- Discover available indexes. Always call first to understand what data is available.
2. `list_splunk_sourcetypes(index='X')` -- Find log types within a specific index.
3. `search_splunk(query='SPL query', earliest_time='-1h')` -- Execute SPL queries to search logs.

### Common SPL Patterns
- Error search: `search_splunk(query='index=X error | stats count by host', earliest_time='-1h')`
- HTTP errors: `search_splunk(query='index=X status>=500 | head 50', earliest_time='-30m')`
- Group by field: `search_splunk(query='index=X | stats count by source', earliest_time='-1h')`
- Time chart: `search_splunk(query='index=X error | timechart count', earliest_time='-6h')`

### SPL Tips
- Use `| head N` to limit results.
- Use `| stats count by FIELD` to aggregate.
- Use `| timechart` to see trends over time.
- The query is automatically prefixed with `search` if it does not start with `search` or `|`.
- `max_count` defaults to 100 results; increase for broader searches.

## RCA Investigation Workflow

**Step 1 -- Discover data:**
`list_splunk_indexes()` -- find which indexes contain relevant logs.

**Step 2 -- Identify log types:**
`list_splunk_sourcetypes(index='main')` -- understand what sourcetypes exist in the target index.

**Step 3 -- Search for errors:**
`search_splunk(query='index=main error OR exception | stats count by host sourcetype', earliest_time='-1h')`

**Step 4 -- Narrow to specific timeframe:**
Use `earliest_time` and `latest_time` to focus on the alert window. Example: `earliest_time='-30m'`.

**Step 5 -- Correlate with infrastructure:**
After Splunk analysis, correlate findings with cloud resources if cloud providers are connected.

## Important Rules
- Splunk is a REMOTE service. Never try to access Splunk data from the local filesystem.
- Always start with `list_splunk_indexes` to discover available data before searching.
- Use targeted queries with specific indexes for faster results.
- Results are truncated at 2MB. Use `| head N` or more specific queries to stay within limits.
- Index and sourcetype names only allow alphanumeric characters, underscores, and hyphens.
