---
name: datadog
id: datadog
description: "Datadog monitoring integration for querying logs, metrics, monitors, events, traces, hosts, and incidents during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.datadog_tool
  function: is_datadog_connected
tools:
  - query_datadog
index: "Full-stack monitoring -- query logs, metrics, monitors, events, traces, hosts, incidents"
rca_priority: 3
allowed-tools: query_datadog
metadata:
  author: aurora
  version: "1.0"
---

# Datadog Integration

## Overview
Datadog integration for querying observability data during Root Cause Analysis. Datadog is a REMOTE service. Use ONLY the `query_datadog` API tool. All data is accessed via a single unified tool with `resource_type` parameter.

## Instructions

### Tool Usage
`query_datadog(resource_type=TYPE, query=QUERY, time_from=START, time_to=END, limit=N)`

### Resource Types
1. `'logs'` -- Search log entries. query=Datadog log query syntax e.g. `"service:web status:error"`
2. `'metrics'` -- Query metric timeseries. query=metric query e.g. `"avg:system.cpu.user{*}"`
3. `'monitors'` -- List monitors with status. query=name filter (optional)
4. `'events'` -- Platform events. query=source filter (optional)
5. `'traces'` -- APM spans/traces. query=span query e.g. `"service:web @http.status_code:500"`
6. `'hosts'` -- Infrastructure hosts. query=host filter (optional)
7. `'incidents'` -- Datadog incidents. Lists active/recent incidents (requires Incident Management; may 403 if not enabled).

### Datadog Query Syntax
- Filter by service: `service:X`
- Filter by status: `status:error`
- HTTP status codes: `@http.status_code:5*`
- Filter by host: `host:X`
- Filter by environment: `env:production`

### Examples
- Logs: `query_datadog(resource_type='logs', query='service:web status:error', time_from='-1h')`
- Metrics: `query_datadog(resource_type='metrics', query='avg:system.cpu.user{*}', time_from='-2h')`
- Traces: `query_datadog(resource_type='traces', query='service:web @http.status_code:500', time_from='-1h')`
- Monitors: `query_datadog(resource_type='monitors', query='web')`

## RCA Investigation Workflow

**Step 1 -- Search logs for errors around the alert time:**
`query_datadog(resource_type='logs', query='service:affected-service status:error', time_from='-1h')`

**Step 2 -- Check traces for failing requests and latency:**
`query_datadog(resource_type='traces', query='service:affected-service @http.status_code:500', time_from='-1h')`

**Step 3 -- Query metrics for resource correlation (CPU, memory, error rates):**
`query_datadog(resource_type='metrics', query='avg:system.cpu.user{host:X}', time_from='-2h')`

**Step 4 -- List monitors to understand alerting context:**
`query_datadog(resource_type='monitors')`

**Step 5 -- Check hosts for infrastructure health:**
`query_datadog(resource_type='hosts', time_from='-1h')`

**Step 6 -- Review incidents for related/correlated issues:**
`query_datadog(resource_type='incidents')`

## Important Rules
- Datadog is a REMOTE service. Use ONLY the `query_datadog` API tool.
- The `resource_type` parameter is required and must be one of: logs, metrics, monitors, events, traces, hosts, incidents.
- Time parameters accept relative strings (`'-1h'`, `'-24h'`, `'-7d'`) or ISO 8601 timestamps.
- The `incidents` resource type requires Datadog Incident Management to be enabled; may return 403 if not.
- Results are truncated at the output size limit. Use more specific queries to narrow results.
