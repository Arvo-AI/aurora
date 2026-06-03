---
name: cloudbees_oc
id: cloudbees_oc
description: "CloudBees Operations Center integration for cross-controller deployment visibility and feature flag correlation during RCA"
category: cicd
connection_check:
  method: get_token_data
  provider_key: cloudbees_oc
  required_field: base_url
tools:
  - cloudbees_rca
index: "CI/CD -- CloudBees Operations Center: cross-controller deployments, managed controller inventory, feature flag changes"
rca_priority: 3
allowed-tools: cloudbees_rca
metadata:
  author: aurora
  version: "1.0"
---

# CloudBees Operations Center Integration

## Overview
CloudBees Operations Center (CJOC) integration for enterprise-wide CI/CD visibility during Root Cause Analysis.
Operations Center manages multiple Jenkins controllers and provides a unified view of all builds and deployments across the organization.

## Instructions

### Tool: cloudbees_rca

The same `cloudbees_rca` tool is used, with additional enterprise actions enabled when Operations Center is connected.

**Enterprise Actions:**
- `controller_list` — List all managed Jenkins controllers and their status (online, offline, failing)
- `cross_controller_deployments` — Query recent builds across ALL managed controllers; optional `service` filter and `time_window_hours`
- `flag_changes` — Query recent feature flag changes from CloudBees Feature Management; optional `app_id` filter

### RCA Investigation Flow

When Operations Center is connected, start broad and narrow down:

1. `cloudbees_rca(action='controller_list')` — See all managed controllers and their health
2. `cloudbees_rca(action='cross_controller_deployments', service='SERVICE')` — Find recent deployments across ALL controllers
3. `cloudbees_rca(action='flag_changes')` — Check if any feature flags were toggled near the incident time
4. Then drill into a specific build using `build_detail`, `pipeline_stages`, `build_logs` as needed

### When to Use

- **Deployment-related incidents**: Use `cross_controller_deployments` to see all recent activity across the entire CI/CD infrastructure, not just one controller.
- **Feature flag incidents**: Use `flag_changes` when an incident might be caused by a feature flag toggle (gradual rollout gone wrong, kill switch activated, etc.).
- **Controller health**: Use `controller_list` to check if a controller itself is unhealthy (offline, failing builds).

### Important Rules
- Always start with `cross_controller_deployments` for broad visibility before drilling into a specific controller.
- Use `flag_changes` when the incident pattern suggests a gradual rollout or toggle (e.g., affecting a percentage of users).
- Controller URLs returned by `controller_list` can be used with standard CloudBees CI actions (`build_detail`, etc.).
