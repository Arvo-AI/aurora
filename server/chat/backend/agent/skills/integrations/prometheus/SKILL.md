---
name: prometheus
id: prometheus
description: "Prometheus monitoring integration for querying metrics via PromQL, checking alerts, targets, and rules during RCA investigations. Includes Alertmanager for alert management and silencing."
category: observability
connection_check:
  method: get_token_data
  provider_key: prometheus
  required_field: prometheus_url
tools:
  - query_prometheus
  - manage_alertmanager
index: "PromQL metrics queries -- alerts, targets health, recording/alerting rules, metric metadata, Alertmanager silences"
rca_priority: 3
allowed-tools: query_prometheus, manage_alertmanager
metadata:
  author: aurora
  version: "1.1"
---

# Prometheus Integration

## Overview
Prometheus integration for querying observability data during Root Cause Analysis. Prometheus is a REMOTE time-series database. Use ONLY the `query_prometheus` and `manage_alertmanager` API tools. All data is accessed via unified tools with action/resource_type parameters.

## Instructions

### Tool Usage
`query_prometheus(resource_type=TYPE, query=QUERY, time_from=START, time_to=END, step=STEP, limit=N)`
`manage_alertmanager(action=ACTION, matchers=MATCHERS, duration_minutes=N, comment=TEXT)`

### Resource Types (query_prometheus)
1. `'metrics'` -- Range PromQL query (time series over interval). query=PromQL expression
2. `'instant'` -- Instant PromQL query (current snapshot). query=PromQL expression
3. `'alerts'` -- Currently firing alerts from Prometheus rules. No query needed.
4. `'rules'` -- Alerting and recording rules. query='alert' or 'record' to filter (optional).
5. `'targets'` -- Scrape targets and health status. query='active', 'dropped', or 'any' (optional).
6. `'metadata'` -- Metric metadata (type, help, unit). query=metric name filter (optional).

### Alertmanager Actions (manage_alertmanager)
1. `'list_alerts'` -- All currently firing alerts from Alertmanager (includes silenced/inhibited state). matchers=optional filter.
2. `'list_silences'` -- Active silences currently suppressing alerts.
3. `'create_silence'` -- Silence alerts matching labels. REQUIRES matchers (e.g. 'alertname=HighCPU,namespace=prod').
4. `'expire_silence'` -- Remove a silence early. REQUIRES silence_id.

### PromQL Query Patterns
- Error rate: `rate(http_requests_total{status=~"5.."}[5m])`
- Request rate: `rate(http_requests_total[5m])`
- Latency P99: `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))`
- CPU usage: `rate(container_cpu_usage_seconds_total[5m])`
- Memory: `container_memory_usage_bytes / container_spec_memory_limit_bytes`
- Disk I/O: `rate(node_disk_io_time_seconds_total[5m])`
- Network errors: `rate(node_network_receive_errs_total[5m])`
- Pod restarts: `increase(kube_pod_container_status_restarts_total[1h])`
- Node availability: `up{job="node-exporter"}`

### Label Filtering
- By namespace: `{namespace="production"}`
- By service/job: `{job="api-server"}`
- By pod: `{pod=~"api-.*"}`
- By instance: `{instance="10.0.0.1:9090"}`
- Regex match: `{status=~"5.."}`
- Negation: `{status!="200"}`

### Examples
- Error rate by service: `query_prometheus(resource_type='metrics', query='sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)', time_from='1h')`
- Current CPU: `query_prometheus(resource_type='instant', query='rate(container_cpu_usage_seconds_total{namespace="production"}[5m])')`
- Firing alerts: `query_prometheus(resource_type='alerts')`
- Target health: `query_prometheus(resource_type='targets', query='active')`
- Find metrics: `query_prometheus(resource_type='metadata', query='http_requests')`
- List Alertmanager alerts: `manage_alertmanager(action='list_alerts')`
- Silence noisy alert: `manage_alertmanager(action='create_silence', matchers='alertname=HighCPU,namespace=production', duration_minutes=30, comment='Silencing during RCA - symptom of root cause')`
- Remove silence: `manage_alertmanager(action='expire_silence', silence_id='abc-123')`

## RCA Investigation Workflow

**Step 1 -- Check Alertmanager for full alert picture:**
`manage_alertmanager(action='list_alerts')`
This shows ALL firing alerts including their silenced/inhibited state — richer than Prometheus /alerts.

**Step 2 -- Check target health (are services UP or DOWN?):**
`query_prometheus(resource_type='targets', query='active')`

**Step 3 -- Query error rates around the alert time:**
`query_prometheus(resource_type='metrics', query='sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)', time_from='1h')`

**Step 4 -- Check latency for the affected service:**
`query_prometheus(resource_type='metrics', query='histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service="AFFECTED"}[5m])) by (le))', time_from='1h')`

**Step 5 -- Check resource saturation (CPU, memory):**
`query_prometheus(resource_type='metrics', query='rate(container_cpu_usage_seconds_total{namespace="production"}[5m])', time_from='1h')`

**Step 6 -- Check for pod restarts or OOMKills:**
`query_prometheus(resource_type='metrics', query='increase(kube_pod_container_status_restarts_total{namespace="production"}[1h])', time_from='2h')`

**Step 7 -- Review alerting rules to understand monitoring coverage:**
`query_prometheus(resource_type='rules', query='alert')`

**Step 8 -- Silence symptom alerts to reduce noise (if root cause identified):**
`manage_alertmanager(action='create_silence', matchers='alertname=HighErrorRate,namespace=production', duration_minutes=60, comment='Symptom of [root cause]. Silencing during remediation.')`

## Alertmanager Silence Guidelines
- Only silence alerts that are SYMPTOMS of the identified root cause, not the root cause alert itself.
- Always include a meaningful comment explaining WHY the alert is being silenced.
- Use the shortest reasonable duration (default 60 min, max 24 hours).
- After remediation, expire silences early rather than letting them time out.
- Never silence alerts preemptively — only after confirming they are noise during active investigation.

## Important Rules
- Prometheus is a REMOTE service. Use ONLY the `query_prometheus` and `manage_alertmanager` API tools.
- The `resource_type` parameter is required for query_prometheus.
- The `action` parameter is required for manage_alertmanager.
- For `metrics` (range queries), always specify `time_from`. Default step is 60s.
- For `instant` queries, the result is evaluated at `time_to` (default: now).
- PromQL expressions must be valid. Use rate() for counters, don't use rate() on gauges.
- Results are truncated at the output size limit. Use more specific label selectors to narrow results.
- Use `by (label)` aggregation to reduce series count when querying high-cardinality metrics.
- manage_alertmanager is only available if the user configured an Alertmanager URL during setup.
