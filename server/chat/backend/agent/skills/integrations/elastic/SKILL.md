---
name: elastic
id: elastic
description: "Elastic Cloud (Elasticsearch + Kibana) integration for discovering indices, searching logs with Lucene or ES|QL, and reading Kibana alerts during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.elastic_tool
  function: is_elastic_connected
tools:
  - elastic_list_indices
  - elastic_get_fields
  - elastic_search_logs
  - elastic_esql
  - elastic_get_alerts
index: "Log analytics -- discover indices, inspect fields, search logs (Lucene / ES|QL), read Kibana alerts"
rca_priority: 3
allowed-tools: elastic_list_indices, elastic_get_fields, elastic_search_logs, elastic_esql, elastic_get_alerts
metadata:
  author: aurora
  version: "1.0"
---

# Elastic Cloud Integration

## Overview
Elastic Cloud integration for querying logs and Kibana alerts during Root Cause Analysis. Elasticsearch is a REMOTE service -- do NOT search the local filesystem for Elastic data. Use ONLY the tools listed below. All tools are READ-ONLY: Aurora never writes to Elasticsearch or Kibana.

Works with Elastic Cloud Hosted, Elastic Cloud Serverless and self-managed clusters.

## Instructions

### Tool Usage (use in this order)
1. `elastic_list_indices(pattern='logs-*')` -- Discover data streams / indices. Call first to learn what data exists (e.g. `logs-nginx.access-default`, `filebeat-*`, `.alerts-*`).
2. `elastic_get_fields(index='logs-*', prefix='kubernetes.')` -- Learn the field names before writing filters (ECS fields: `log.level`, `service.name`, `host.name`, `kubernetes.pod.name`, `error.message`, `http.response.status_code`).
3. `elastic_search_logs(query='log.level:error', index='logs-*', time_range='1h')` -- Lucene search, newest first, compact documents.
4. `elastic_esql(query='FROM logs-* | WHERE log.level == "error" | STATS count() BY service.name')` -- Aggregations, top-N, and timelines.
5. `elastic_get_alerts(status='active', hours=24)` -- Kibana alert documents (what fired, when, why).

### Lucene (query_string) cheat sheet for `elastic_search_logs`
- Errors: `log.level:error OR log.level:critical`
- One service: `service.name:"checkout" AND log.level:error`
- Text search: `message:"connection refused"` or `message:timeout*`
- Kubernetes: `kubernetes.namespace:"prod" AND kubernetes.pod.name:api-*`
- HTTP 5xx: `http.response.status_code:[500 TO 599]`
- Exclude noise: `NOT message:healthcheck`
- Absolute window: `start_time='2026-01-05T10:00:00Z'`, `end_time='2026-01-05T10:30:00Z'`
- Use `fields=['@timestamp','message','host.name']` to keep responses small.

### ES|QL cheat sheet for `elastic_esql`
- Errors by service: ``FROM logs-* | WHERE log.level == "error" | STATS count() BY service.name | SORT `count()` DESC``
- Errors by host: `FROM logs-* | WHERE log.level IN ("error","critical") | STATS errors = COUNT(*) BY host.name | SORT errors DESC`
- Timeline (5-min buckets): `FROM logs-* | WHERE log.level == "error" | STATS c = COUNT(*) BY bucket = BUCKET(@timestamp, 5 minutes) | SORT bucket`
- Top messages: `FROM logs-* | WHERE log.level == "error" | STATS c = COUNT(*) BY message | SORT c DESC | LIMIT 20`
- Free-text: `FROM logs-* | WHERE MATCH(message, "connection refused") | KEEP @timestamp, service.name, message`
- The `time_range` argument adds a pre-filter on `timestamp_field` (default `@timestamp`) automatically; pass `time_range='all'` to disable it for indices without a date field. `| LIMIT 100` is appended if missing.
- Both `elastic_search_logs` and `elastic_esql` accept `timestamp_field` for indices that do not use `@timestamp` (check with `elastic_get_fields`); with the wrong field the window matches nothing and you get 0 hits, not an error.

### Kibana alert fields (from `elastic_get_alerts`)
`ruleName`, `ruleCategory`, `status` (active|recovered), `reason` (human-readable trigger text), `start`/`end`, `value`/`threshold`, `instanceId` (the group, e.g. host or service), `uuid`.

## RCA Investigation Workflow

**Step 1 -- Anchor on the alert:**
Read `alert_metadata` / the webhook payload (`ruleName`, `reason`, `value`, `threshold`, `viewInAppUrl`). If the incident came from elsewhere, call `elastic_get_alerts(status='active')` to see what Kibana is currently flagging.

**Step 2 -- Discover data:**
`elastic_list_indices(pattern='logs-*')` then `elastic_get_fields(index='<stream>')` for the relevant stream.

**Step 3 -- Find errors in the alert window:**
`elastic_search_logs(query='log.level:error', index='logs-*', time_range='30m')` or with `start_time`/`end_time` around `kibana.alert.start`.

**Step 4 -- Aggregate to find the blast radius:**
`elastic_esql(query='FROM logs-* | WHERE log.level == "error" | STATS c = COUNT(*) BY service.name, host.name | SORT c DESC', time_range='1h')`

**Step 5 -- Build a timeline:**
Bucket errors by 5 minutes (see ES|QL cheat sheet) to find when the failure started; compare with deploys / infra changes.

**Step 6 -- Correlate with infrastructure:**
Use `host.name`, `kubernetes.*`, `cloud.instance.id` values from the logs to pivot into cloud/Kubernetes tools if connected.

## Important Rules
- Elastic is a REMOTE service. Never look for Elastic data on the local filesystem.
- Never attempt to write: no index creation, no document writes, no Kibana Cases or rule changes.
- Results are truncated at 2MB / 500 documents. Prefer aggregations (`elastic_esql` with STATS) over pulling raw documents, and always use `| LIMIT n`.
- Index patterns only allow letters, digits, `_ . - * ,`.
- If a tool returns a 403, the API key lacks privileges for that index or for Kibana -- report it, do not retry repeatedly.
