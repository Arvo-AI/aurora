---
name: jenkins
id: jenkins
description: "Jenkins CI/CD integration for investigating builds, deployments, pipeline stages, and test results during RCA"
category: cicd
connection_check:
  method: get_token_data
  provider_key: jenkins
  required_field: base_url
tools:
  - jenkins_rca
index: "CI/CD -- investigate Jenkins builds, deployments, pipeline stages, logs, test results, OTel traces"
rca_priority: 4
allowed-tools: jenkins_rca
metadata:
  author: aurora
  version: "1.0"
---

# Jenkins Integration

## Overview
Jenkins CI/CD integration for investigating builds and deployments during Root Cause Analysis.
Uses three Jenkins APIs: Core REST API, Pipeline REST API (wfapi), and Blue Ocean REST API.

## Instructions

### Tool: jenkins_rca

Unified Jenkins CI/CD investigation tool for Root Cause Analysis.

**Actions:**
- `recent_deployments` -- Query stored deployment events; optional `service` filter and `time_window_hours`
- `build_detail` -- Core API: SCM revision, changeSets, build causes, parameters. Requires `job_path` + `build_number`
- `pipeline_stages` -- wfapi: stage-level breakdown with status and timing. Requires `job_path` + `build_number`
- `stage_log` -- wfapi: per-stage log output for a specific `node_id`. Requires `job_path` + `build_number` + `node_id`
- `build_logs` -- Core API: console output, truncated to ~1MB. Requires `job_path` + `build_number`
- `test_results` -- Core API: test report with failure details. Requires `job_path` + `build_number`
- `blue_ocean_run` -- Blue Ocean API: run data with changeSet and commit info. Requires `pipeline_name` + `run_number`
- `blue_ocean_steps` -- Blue Ocean API: step-level detail for a pipeline node. Requires `pipeline_name` + `run_number`
- `trace_context` -- Extract OTel W3C Trace Context; params: `deployment_event_id` or `job_path` + `build_number`

**Required params vary by action:** `job_path` + `build_number` for Core/wfapi, `pipeline_name` + `run_number` for Blue Ocean. `service` is optional for `recent_deployments`.

### RCA Investigation Flow

Recent deployments are a leading indicator of root cause. Always check if a deployment occurred shortly before the alert fired.

1. `jenkins_rca(action='recent_deployments', service='SERVICE')` -- Check for recent deploys
2. `jenkins_rca(action='build_detail', job_path='JOB', build_number=N)` -- Build details + commits
3. `jenkins_rca(action='pipeline_stages', job_path='JOB', build_number=N)` -- Stage breakdown
4. `jenkins_rca(action='build_logs', job_path='JOB', build_number=N)` -- Console output
5. `jenkins_rca(action='test_results', job_path='JOB', build_number=N)` -- Test failures
6. `jenkins_rca(action='trace_context', deployment_event_id=ID)` -- OTel trace correlation

### Important Rules
- Always start with `recent_deployments` to find deployments near the incident time.
- Use `build_detail` to get SCM changes and build causes before reading logs.
- Use `pipeline_stages` for stage-level breakdown to narrow which stage failed.
- Use `trace_context` to correlate deployment events with distributed traces.

## Recent Deployments
{jenkins_deploys_section}

## Investigation Commands
- `jenkins_rca(action='recent_deployments', service='{service_name}')` -- Recent deploys
- `jenkins_rca(action='build_detail', job_path='JOB', build_number=N)` -- Build details + commits
- `jenkins_rca(action='pipeline_stages', job_path='JOB', build_number=N)` -- Stage breakdown
- `jenkins_rca(action='stage_log', job_path='JOB', build_number=N, node_id='NODE')` -- Stage logs
- `jenkins_rca(action='build_logs', job_path='JOB', build_number=N)` -- Console output
- `jenkins_rca(action='test_results', job_path='JOB', build_number=N)` -- Test failures
- `jenkins_rca(action='blue_ocean_run', pipeline_name='PIPELINE', run_number=N)` -- Blue Ocean data
- `jenkins_rca(action='trace_context', deployment_event_id=ID)` -- OTel trace correlation

Recent deployments are a leading indicator of root cause.
