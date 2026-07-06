---
name: cloudbees
id: cloudbees
description: "CloudBees CI integration for investigating builds, deployments, pipeline stages, and test results during RCA"
category: cicd
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.cloudbees_rca_tool
  function: is_cloudbees_connected
tools:
  - cloudbees_rca
index: "CI/CD -- investigate CloudBees CI builds, deployments, pipeline stages, logs, test results"
rca_priority: 4
allowed-tools: cloudbees_rca
metadata:
  author: aurora
  version: "2.0"
---

# CloudBees CI Integration

## Overview
CloudBees CI integration for investigating builds and deployments during Root Cause Analysis.
CloudBees CI uses the same APIs as Jenkins: Core REST API, Pipeline REST API (wfapi), and Blue Ocean REST API.

## Instructions

### Tool: cloudbees_rca

Unified CloudBees CI investigation tool for Root Cause Analysis.

**Actions:**
- `recent_deployments` -- Query stored deployment events; optional `service` filter and `time_window_hours`
- `build_detail` -- Core API: SCM revision, changeSets, build causes, parameters. Requires `job_path` + `build_number`
- `pipeline_stages` -- wfapi: stage-level breakdown with status and timing. Requires `job_path` + `build_number`
- `stage_log` -- wfapi: per-stage log output for a specific `node_id`. Requires `job_path` + `build_number` + `node_id`
- `build_logs` -- Core API: console output, truncated to ~1MB. Requires `job_path` + `build_number`
- `test_results` -- Core API: test report with failure details. Requires `job_path` + `build_number`
- `blue_ocean_run` -- Blue Ocean API: run data with changeSet and commit info. Requires `pipeline_name` + `run_number`
- `blue_ocean_steps` -- Blue Ocean API: step-level detail for a pipeline node. Requires `pipeline_name` + `run_number`

**Required params vary by action:** `job_path` + `build_number` for Core/wfapi, `pipeline_name` + `run_number` for Blue Ocean. `service` is optional for `recent_deployments`.

### RCA Investigation Flow

Recent deployments are a leading indicator of root cause. Always check if a deployment occurred shortly before the alert fired.

1. `cloudbees_rca(action='recent_deployments', service='SERVICE')` -- Check for recent deploys
2. `cloudbees_rca(action='build_detail', job_path='JOB', build_number=N)` -- Build details + commits
3. `cloudbees_rca(action='pipeline_stages', job_path='JOB', build_number=N)` -- Stage breakdown
4. `cloudbees_rca(action='build_logs', job_path='JOB', build_number=N)` -- Console output
5. `cloudbees_rca(action='test_results', job_path='JOB', build_number=N)` -- Test failures

### Important Rules
- Always start with `recent_deployments` to find deployments near the incident time.
- Use `build_detail` to get SCM changes and build causes before reading logs.
- Use `pipeline_stages` for stage-level breakdown to narrow which stage failed.
- Do NOT call `flag_changes` unless Feature Management is known to be connected.
- Do NOT call `cross_controller_deployments` or `controller_list` unless Operations Center OR a manually-registered controller fleet (Multiple Controllers mode) is connected.

## Recent Deployments
{cloudbees_deploys_section}

## Investigation Commands
- `cloudbees_rca(action='recent_deployments', service='{service_name}')` -- Recent deploys
- `cloudbees_rca(action='build_detail', job_path='JOB', build_number=N)` -- Build details + commits
- `cloudbees_rca(action='pipeline_stages', job_path='JOB', build_number=N)` -- Stage breakdown
- `cloudbees_rca(action='stage_log', job_path='JOB', build_number=N, node_id='NODE')` -- Stage logs
- `cloudbees_rca(action='build_logs', job_path='JOB', build_number=N)` -- Console output
- `cloudbees_rca(action='test_results', job_path='JOB', build_number=N)` -- Test failures
- `cloudbees_rca(action='blue_ocean_run', pipeline_name='PIPELINE', run_number=N)` -- Blue Ocean data

Recent deployments are a leading indicator of root cause.

## Multi-Controller Actions (Operations Center OR a manually-registered fleet)

These actions work in two cases: when Operations Center is connected (provider: cloudbees_oc),
OR when the user has manually registered multiple standalone controllers via "Multiple Controllers"
mode (provider: cloudbees_fleet — for clients that run several controllers but have no OC):

- `controller_list` — List all controllers and their status (online/offline)
- `cross_controller_deployments` — Query recent builds across ALL controllers

In fleet mode, controllers were registered individually (each with its own URL + token), since
standalone CloudBees CI controllers cannot be discovered automatically without Operations Center.

For per-build introspection (`build_detail`, `pipeline_stages`, `build_logs`, etc.) in either OC or
fleet mode, pass `controller_url` — get the URL from `controller_list` or
`cross_controller_deployments` (the `_controller_url` field) first.

These return a helpful error if neither OC nor a fleet is connected.

## Enterprise Actions (Feature Management)

This action is ONLY available when Feature Management is connected (provider: cloudbees_fm):

- `flag_changes` — Query recent feature flag changes (requires `app_id` parameter)

Only use this if you have confirmed Feature Management is connected AND you have an app_id.
