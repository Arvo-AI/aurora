---
name: opensearch
id: opensearch
description: "OpenSearch log search integration for querying logs by keyword, service, time range, and error pattern during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.opensearch_tool
  function: is_opensearch_connected
tools:
  - search_opensearch
  - list_opensearch_indices
index: "Log analytics -- search OpenSearch logs by query, time range, and index pattern"
rca_priority: 3
allowed-tools: search_opensearch, list_opensearch_indices
metadata:
  author: aurora
  version: "1.0"
---

# OpenSearch Integration

## Overview
OpenSearch integration for querying log data during Root Cause Analysis. OpenSearch is a REMOTE service — do NOT search the local filesystem. Use ONLY the tools listed below.

## Instructions

### Tool Usage (use in this order)
1. `list_opensearch_indices()` — Discover available indices. Call first to understand what data exists.
2. `search_opensearch(query='error', start_time='now-1h')` — Search for logs matching a Lucene query.

### Common Query Patterns
- Error search: `search_opensearch(query='error AND service:api', start_time='now-1h')`
- Specific service: `search_opensearch(query='kubernetes.labels.app:payment-service AND level:error', start_time='now-30m')`
- HTTP 5xx: `search_opensearch(query='http.response.status_code:>=500', start_time='now-1h')`
- Exception: `search_opensearch(query='exception OR stacktrace', start_time='now-2h', end_time='now')`
- Specific index: `search_opensearch(query='NullPointerException', index='app-logs-*', start_time='now-1h')`

### Time Format
- Relative: `now-1h`, `now-30m`, `now-6h`, `now-1d`
- Absolute: ISO-8601 strings (`2024-01-15T10:00:00Z`)

## RCA Investigation Workflow

**Step 1 — Discover indices:**
`list_opensearch_indices()` — find which indices contain logs relevant to the incident.

**Step 2 — Search for errors around the alert time:**
`search_opensearch(query='error OR exception OR fatal', start_time='now-1h', size=50)`

**Step 3 — Narrow by service:**
`search_opensearch(query='service:payment AND level:error', start_time='now-30m')`

**Step 4 — Correlate with the alert window:**
Use `start_time` and `end_time` to focus on the exact incident window.

## Important Rules
- OpenSearch is a REMOTE service. Never try to access data from the local filesystem.
- Always use `list_opensearch_indices` first if you're unsure which index to search.
- Keep queries focused — use specific service names and time ranges to avoid huge result sets.
- Results are capped at 200 hits. Use a more specific query if you need narrower results.
- Query syntax is Lucene: `field:value`, `AND`, `OR`, `NOT`, `field:>=N`, wildcards with `*`.
