---
name: spinnaker
id: spinnaker
description: "Spinnaker CD platform integration for investigating pipeline executions, application health, and triggering rollbacks during RCA"
category: cicd
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.spinnaker_rca_tool
  function: is_spinnaker_connected
tools:
  - spinnaker_rca
index: "CI/CD -- investigate Spinnaker pipelines, application health, trigger rollbacks"
rca_priority: 4
allowed-tools: spinnaker_rca
metadata:
  author: aurora
  version: "1.0"
---

# Spinnaker Integration

## Overview
Spinnaker CD platform integration for investigating pipeline executions and application health during Root Cause Analysis. Use during RCA to check if deployments correlate with incidents.

## Instructions

### Tool: spinnaker_rca

Query Spinnaker CD platform for root cause analysis and interactive investigation.

**Actions:**
- `recent_pipelines` -- List recent pipeline executions; optional `application` filter and `limit`
- `pipeline_detail` -- Get full execution with stage-by-stage status. Requires `execution_id`
- `application_health` -- Cluster + server group health. Requires `application`
- `list_pipeline_configs` -- Available pipeline definitions. Requires `application`
- `trigger_pipeline` -- Trigger a pipeline (e.g. rollback). Requires `application` + `pipeline_name`, optional `parameters`
- `execution_logs` -- Detailed logs for failed stages. Requires `execution_id`

### RCA Investigation Flow

1. `spinnaker_rca(action='recent_pipelines', application='APP')` -- Check for recent pipeline executions
2. `spinnaker_rca(action='pipeline_detail', execution_id='ID')` -- Get stage-by-stage status for a suspicious execution
3. `spinnaker_rca(action='application_health', application='APP')` -- Check cluster and server group health
4. `spinnaker_rca(action='execution_logs', execution_id='ID')` -- Get detailed logs for failed stages
5. `spinnaker_rca(action='list_pipeline_configs', application='APP')` -- List available pipelines (e.g. to find a rollback pipeline)
6. `spinnaker_rca(action='trigger_pipeline', application='APP', pipeline_name='rollback')` -- Trigger rollback if needed

### Important Rules
- Check `recent_pipelines` first to correlate deployments with incident timing.
- Use `pipeline_detail` to get stage-by-stage breakdown before reading logs.
- Use `application_health` to assess current cluster state.
- Only use `trigger_pipeline` (e.g. for rollback) after confirming root cause with the user.
