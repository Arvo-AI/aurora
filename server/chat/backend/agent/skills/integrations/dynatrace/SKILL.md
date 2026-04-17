---
name: dynatrace
id: dynatrace
description: "Dynatrace APM integration for querying problems, logs, metrics, and monitored entities during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.dynatrace_tool
  function: is_dynatrace_connected
tools:
  - query_dynatrace
index: "APM platform -- query problems, logs, metrics, entities via Dynatrace API"
rca_priority: 3
allowed-tools: query_dynatrace
metadata:
  author: aurora
  version: "1.0"
---

# Dynatrace Integration

## Overview
Dynatrace integration for querying APM data during Root Cause Analysis. Dynatrace is a REMOTE service. Use ONLY the `query_dynatrace` API tool. All data is accessed via a single unified tool with `resource_type` parameter.

## Instructions

### Tool Usage
`query_dynatrace(resource_type=TYPE, query=SELECTOR, time_from=START, time_to=END, limit=N)`

### Resource Types
1. `'problems'` -- Active/recent problems. query=problem selector e.g. `status("open")`
2. `'entities'` -- Monitored hosts/services/processes. query=entity selector e.g. `type("HOST")`
3. `'logs'` -- Log entries. query=search string
4. `'metrics'` -- Metric time series. query=metric selector e.g. `builtin:host.cpu.usage`

### Selector Syntax
- Problem selectors: `status("open")`, `status("closed")`, `severityLevel("ERROR")`
- Entity selectors: `type("HOST")`, `type("SERVICE")`, `type("APPLICATION")`, `type("PROCESS_GROUP")`
- Metric selectors: `builtin:host.cpu.usage`, `builtin:host.mem.usage`, `builtin:service.response.time`

### Time Format
- Use Dynatrace relative time: `now-1h`, `now-2h`, `now-24h`
- Default time_from is `now-2h`

### Examples
- Problems: `query_dynatrace(resource_type='problems', query='status("open")', time_from='now-1h')`
- Metrics: `query_dynatrace(resource_type='metrics', query='builtin:host.cpu.usage', time_from='now-30m')`
- Entities: `query_dynatrace(resource_type='entities', query='type("HOST")', time_from='now-1h')`
- Logs: `query_dynatrace(resource_type='logs', query='error', time_from='now-1h')`

## RCA Investigation Workflow

**Step 1 -- Check active problems:**
Start with problems to understand the current issue landscape.
`query_dynatrace(resource_type='problems', query='status("open")', time_from='now-2h')`

**Step 2 -- Drill into affected entities:**
Identify which hosts, services, or processes are impacted.
`query_dynatrace(resource_type='entities', query='type("HOST")', time_from='now-2h')`

**Step 3 -- Search logs for errors:**
Find error messages around the alert time.
`query_dynatrace(resource_type='logs', query='ERROR', time_from='now-1h')`

**Step 4 -- Query metrics for resource patterns:**
Check CPU, memory, and response time metrics for anomalies.
`query_dynatrace(resource_type='metrics', query='builtin:host.cpu.usage', time_from='now-1h')`

## Important Rules
- Dynatrace is a REMOTE service. Use ONLY the `query_dynatrace` API tool.
- The `resource_type` parameter is required and must be one of: problems, logs, metrics, entities.
- Start with `problems` to understand the issue, then drill into entities and logs.
- Token scopes matter: `logs.read` is required for log queries, `metrics.read` for metric queries. A 403 error indicates a missing scope.
- A 400 error typically means an invalid selector query. Check the syntax.
- Results are truncated at the output size limit. Use narrower time ranges to stay within limits.
