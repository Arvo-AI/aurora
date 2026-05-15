---
name: coroot
id: coroot
description: "Coroot eBPF-powered observability integration for kernel-level infrastructure monitoring, incidents, traces, logs, service maps, deployments, nodes, costs, and risks"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.coroot_tool
  function: is_coroot_connected
tools:
  - coroot_get_incidents
  - coroot_get_incident_detail
  - coroot_get_applications
  - coroot_get_app_detail
  - coroot_get_app_logs
  - coroot_get_traces
  - coroot_get_service_map
  - coroot_query_metrics
  - coroot_get_deployments
  - coroot_get_nodes
  - coroot_get_overview_logs
  - coroot_get_node_detail
  - coroot_get_costs
  - coroot_get_risks
index: "eBPF kernel-level observability -- incidents, apps, logs, traces, service map, metrics, nodes, deployments, costs, risks"
rca_priority: 3
allowed-tools: coroot_get_incidents, coroot_get_incident_detail, coroot_get_applications, coroot_get_app_detail, coroot_get_app_logs, coroot_get_traces, coroot_get_service_map, coroot_query_metrics, coroot_get_deployments, coroot_get_nodes, coroot_get_overview_logs, coroot_get_node_detail, coroot_get_costs, coroot_get_risks
metadata:
  author: aurora
  version: "1.0"
---

# Coroot Integration

## Overview
Coroot is an eBPF-powered observability platform. Its node agent instruments at the KERNEL level, capturing data that applications cannot self-report and requires NO code changes or SDK integration.

### What eBPF Gives You (data invisible to application logs)
- **TCP connections:** every connect/accept/close between services, including failed connects and retransmissions
- **Network latency:** actual round-trip time measured at the kernel, not application-reported
- **DNS queries:** every resolution with latency, NXDOMAIN errors, and server failures
- **Disk I/O:** per-process read/write latency and throughput at the block device level
- **Container resources:** CPU usage, memory RSS, OOM kills, throttling -- from cgroups
- **L7 protocol parsing:** HTTP, PostgreSQL, MySQL, Redis, MongoDB, Memcached request/response metrics extracted from TCP streams without application instrumentation
- **Service map:** automatically discovered from observed TCP connections -- not configured manually

### Issues Coroot Sees BEFORE Application Logs
- A service failing to connect to a dependency (TCP connect failures)
- Network packet loss and retransmissions between pods/nodes
- DNS resolution failures causing timeouts
- Disk I/O saturation causing slow queries
- OOM kills that happen before the app can log anything
- Container CPU throttling invisible to the application

## Instructions

### Incident Investigation Flow
1. `coroot_get_incidents(lookback_hours=24)` -- List incidents with RCA summaries, root cause, and fixes
2. `coroot_get_overview_logs(severity='Error', limit=50)` -- Search all logs cluster-wide for errors (includes Kubernetes Events: OOMKilled, Evicted, CrashLoopBackOff, FailedScheduling)
3. `coroot_get_incident_detail(incident_key='KEY')` -- Full incident detail with propagation map
4. `coroot_get_app_detail(app_id='ID')` -- Audit reports for affected app (35+ health checks)
5. `coroot_get_app_logs(app_id='ID', severity='Error')` -- Error logs with trace correlation
6. `coroot_get_traces(service_name='svc', status_error=True)` -- Error traces across services
7. `coroot_get_traces(trace_id='ID')` -- Full trace tree for a specific request

### Proactive Health Scan
1. `coroot_get_applications()` -- All apps sorted by status (CRITICAL first)
2. `coroot_get_service_map()` -- Auto-discovered dependencies from eBPF TCP tracking
3. `coroot_get_deployments(lookback_hours=24)` -- Correlate deploys with failures
4. `coroot_get_risks()` -- Security and availability risks (single-instance, single-AZ, exposed ports)

### Node Investigation
1. `coroot_get_nodes()` -- List all nodes with health status
2. `coroot_get_node_detail(node_name='NODE')` -- Full audit (CPU, memory, disk, network per-interface)

### Cost Investigation
1. `coroot_get_costs(lookback_hours=24)` -- Cost breakdown per node/app + right-sizing recommendations (cost spikes correlate with autoscaling issues, memory leaks, retry storms)

### PromQL Metrics (all collected by eBPF, no exporters needed)
`coroot_query_metrics(promql='rate(container_resources_cpu_usage_seconds_total[5m])')`

Key queries: CPU, memory RSS, OOM kills, HTTP error rate, TCP connect failures, TCP retransmissions, network RTT, DNS latency, DB query latency, container restarts.

### Status Codes
- 0 = UNKNOWN
- 1 = OK
- 2 = INFO
- 3 = WARNING
- 4 = CRITICAL

## RCA Investigation Workflow

**Step 1 -- Check incidents:**
`coroot_get_incidents(lookback_hours=24)` -- get recent incidents with built-in RCA.

**Step 2 -- Cluster-wide error logs:**
`coroot_get_overview_logs(severity='Error', limit=50)` -- find errors across all apps. Call with `kubernetes_only=True` separately to get K8s events.

**Step 3 -- Incident detail:**
`coroot_get_incident_detail(incident_key='KEY')` -- full RCA with propagation map for a specific incident.

**Step 4 -- Application deep dive:**
`coroot_get_app_detail(app_id='ID')` -- 22 report types, 35+ health checks from eBPF. Detects OOM kills, TCP failures, disk I/O saturation, CPU throttling, DNS errors, network packet loss, DB connection pool exhaustion.

**Step 5 -- Application logs:**
`coroot_get_app_logs(app_id='ID', severity='Error')` -- filtered logs with trace IDs for correlation.

**Step 6 -- Distributed traces:**
`coroot_get_traces(service_name='svc', status_error=True)` -- error traces across services.

**Step 7 -- Correlate with deployments:**
`coroot_get_deployments(lookback_hours=24)` -- check if a deployment correlates with the failure.

**Step 8 -- Infrastructure nodes:**
`coroot_get_nodes()` then `coroot_get_node_detail(node_name='NODE')` for WARNING/CRITICAL nodes.

## Important Rules
- Check Coroot FIRST for any infrastructure-layer issue -- it sees kernel-level events that application logs and cloud provider metrics cannot capture.
- Use `coroot_get_overview_logs` for cluster-wide search when you don't know which app is affected. Use `coroot_get_app_logs` when you already know the target app.
- The `project_id` parameter is auto-detected if omitted. Only pass it when targeting a specific Coroot project.
- All `lookback_hours` values are clamped to a maximum of 720 hours (30 days).
- Results are truncated at 120,000 characters. Use filters or shorter lookback periods to narrow results.
- Metric datapoints are trimmed to the most recent 120 per series.
