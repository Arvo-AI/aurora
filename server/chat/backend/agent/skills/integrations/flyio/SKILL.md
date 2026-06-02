---
name: flyio
id: flyio
description: "Fly.io integration for application monitoring, machine lifecycle management, metrics, logs, and incident remediation"
category: cloud_provider
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.flyio_tool
  function: is_flyio_connected
tools:
  - cloud_exec
  - query_flyio_metrics
index: "Fly.io — apps, machines, logs, metrics, deployments, health checks, remediation"
rca_priority: 8
allowed-tools: cloud_exec, query_flyio_metrics
metadata:
  author: aurora
  version: "1.0"
---

# Fly.io Integration

## Overview
Fly.io is connected for application monitoring, machine lifecycle management, and incident remediation.

## Instructions

### How to interact
- Use `cloud_exec('flyio', '<fly command>')` for all Fly.io CLI operations.
- The CLI (`flyctl`) is pre-authenticated with the user's org-scoped token.
- Always pass `--json` flag for structured output when available.

### Prometheus metrics
Use `query_flyio_metrics(query)` for PromQL queries against Fly.io's Prometheus federation. Common metrics include (but are not limited to) `fly_instance_up`, `fly_instance_cpu`, `fly_instance_memory_resident`, `fly_edge_http_responses_count`, `fly_edge_http_response_time_seconds_bucket`, `fly_instance_net_recv_bytes`, `fly_app_concurrency`.

### Critical rules
- Always use `cloud_exec('flyio', ...)` -- never call the REST API directly.
- Use `-a <app_name>` for commands that target a specific app (e.g. `fly status -a myapp`). Do NOT use `-a` on global commands like `fly apps list`, `fly regions list`.
- Pass `--json` for structured output when available (not all commands support it -- if it fails, retry without).
- For logs, always use `--no-tail` to avoid indefinite streaming (e.g. `fly logs -a myapp --no-tail`).
