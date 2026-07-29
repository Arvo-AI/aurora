---
name: datadog
id: datadog
description: "Datadog monitoring integration for querying logs, metrics, monitors, events, traces, hosts, and incidents during RCA investigations"
category: observability
connection_check:
  method: get_token_data
  provider_key: datadog
  required_any_fields: [api_key, apiKey]
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
`query_datadog(resource_type=TYPE, query=QUERY, time_from=START, time_to=END, limit=N, interval=MS)`

### Resource Types
1. `'logs'` -- Search log entries. query=Datadog log query syntax e.g. `"service:web status:error"`
2. `'metrics'` -- Query metric timeseries (raw points). query=metric query e.g. `"avg:system.cpu.user{*}"`
3. `'metric_stats'` -- Percentile summary per series (p50/p95/p99/max/mean). Same metric query
   syntax as `'metrics'`, but returns one compact row per series instead of raw points.
   Use this for capacity and right-sizing questions over long windows.
4. `'monitors'` -- List monitors with status. query=name filter (optional)
5. `'events'` -- Platform events. query=source filter (optional)
6. `'traces'` -- APM spans/traces. query=span query e.g. `"service:web @http.status_code:500"`
7. `'hosts'` -- Infrastructure hosts. query=host filter (optional)
8. `'incidents'` -- Datadog incidents. Lists active/recent incidents (requires Incident Management; may 403 if not enabled).

### The `interval` Parameter
`interval` is the rollup granularity in **milliseconds**, and applies to `'metrics'` and
`'metric_stats'`. Omit it and a granularity is auto-picked that keeps each series under
~1000 points -- a 30-day window auto-picks `3600000` (1 hour, 720 points). Values are
clamped to `60000`..`14400000`. Datadog caps a series at 1500 points, so a long window
with a fine interval returns less than you asked for; prefer the auto-pick.

### Percentiles
**Datadog cannot compute a time-percentile.** Do not attempt any of these -- every one
is rejected or silently empty:
- `.rollup(percentile, 95, 3600)` and `.rollup(p95, 3600)` -- `400 Unrecognized rollup method`.
  `.rollup()` accepts only `avg`, `sum`, `min`, `max`, `count`.
- `p95:my.metric{...}` -- returns `200` with **zero series** for gauges. The `pXX:` prefix
  needs *distribution* metrics; `kubernetes.cpu.usage.total` and `kubernetes.memory.usage`
  are gauges, so it can never apply to them.
- `formula: "p95(a)"` -- `400 function "p95()" does not exist`.
- `formula: "percentile(a, 95, 3600)"` -- `percentile()` exists but is a **space** aggregator:
  arguments 2 and 3 are *group tags*, not a percentile value and window.
- scalar `aggregator: "percentile"` or `"p95"` -- `400`.

Use `resource_type='metric_stats'` instead. It fetches the rolled-up points and computes
percentiles server-side, using **nearest-rank** order statistics -- so every reported p95
is a value that actually occurred and a reviewer can find it in the Datadog UI.

Each row carries `points` and `nulls`. **Always check them.** Datadog emits nulls for gaps,
and a row with `p95: null` plus a `note` means *no data*, which is not the same as *low
usage* -- never treat an empty series as an idle workload.

### Reconciliation vs Sizing
These are two different questions and must not be conflated:
- **Reconciliation** -- "does our view match the team's?" Read the org's own monitor
  definitions with `resource_type='monitors'` to get their real `query` strings and
  `options.thresholds`, then reproduce that formula exactly. This tells you which
  workloads run hot. It is *not* the basis of any recommended number.
- **Sizing** -- "what should this value be?" Use `resource_type='metric_stats'` for the
  usage distribution over the window. Never derive a sizing number from a monitor threshold.

### Datadog Query Syntax
- Filter by service: `service:X`
- Filter by status: `status:error`
- HTTP status codes: `@http.status_code:5*`
- Filter by host: `host:X`
- Filter by environment: `env:production`

### Examples
- Logs: `query_datadog(resource_type='logs', query='service:web status:error', time_from='-1h')`
- Metrics: `query_datadog(resource_type='metrics', query='avg:system.cpu.user{*}', time_from='-2h')`
- Metric stats (30-day p95 per deployment, one query for all of them):
  `query_datadog(resource_type='metric_stats', query='sum:kubernetes.memory.usage{env:production} by {kube_deployment}', time_from='-30d')`
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
- The `resource_type` parameter is required and must be one of: logs, metrics, metric_stats, monitors, events, traces, hosts, incidents.
- Time parameters accept relative strings (`'-1h'`, `'-24h'`, `'-7d'`) or ISO 8601 timestamps.
- The `incidents` resource type requires Datadog Incident Management to be enabled; may return 403 if not.
- Results are truncated at the output size limit. Use more specific queries to narrow results.
- Never reason from a truncated result set. If a response carries `truncated`, `truncated_all`,
  `series_truncated` or `series_dropped`, narrow the query and re-run before drawing a conclusion.
- Group with `by {tag}` rather than issuing one query per workload -- a single grouped query
  returns every deployment at once. A 30-day multi-group query is close to the request timeout,
  so query one environment at a time.
