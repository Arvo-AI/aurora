---
name: opsgenie
id: opsgenie
description: "OpsGenie / JSM Operations integration for querying alerts, incidents, services, on-call schedules, and teams during RCA investigations"
category: incident_management
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.opsgenie_tool
  function: is_opsgenie_connected
tools:
  - query_opsgenie
index: "Incident management -- query alerts, incidents, services, on-call schedules, teams"
rca_priority: 2
allowed-tools: query_opsgenie
metadata:
  author: aurora
  version: "1.0"
---

# OpsGenie / JSM Operations Integration

## Overview
OpsGenie (or Jira Service Management Operations) integration for querying alert and incident data during Root Cause Analysis. Use ONLY the `query_opsgenie` tool. All data is accessed via a single unified tool with `resource_type` parameter.

## Instructions

### Tool Usage
`query_opsgenie(resource_type=TYPE, query=QUERY, identifier=ID, time_from=START, time_to=END, limit=N)`

### Resource Types
1. `'alerts'` -- List alerts. query=OpsGenie query syntax e.g. `"status=open AND priority=P1"`
2. `'alert_details'` -- Get full alert with logs and notes. identifier=alert ID (required)
3. `'incidents'` -- List incidents. query=OpsGenie query syntax (optional)
4. `'incident_details'` -- Get incident with timeline. identifier=incident ID (required)
5. `'services'` -- List registered services
6. `'on_call'` -- Get on-call participants. identifier=schedule ID (optional; omit to list all)
7. `'schedules'` -- List on-call schedules
8. `'teams'` -- List teams

### OpsGenie Query Syntax
- Filter by status: `status=open`
- Filter by priority: `priority=P1`
- Combine filters: `status=open AND priority=P1`
- Filter by tag: `tag=production`
- Filter by team: `responders=team-name`

### Examples
- Open P1 alerts: `query_opsgenie(resource_type='alerts', query='status=open AND priority=P1')`
- Alert details: `query_opsgenie(resource_type='alert_details', identifier='alert-id-here')`
- Recent incidents: `query_opsgenie(resource_type='incidents', time_from='-24h')`
- Who is on call: `query_opsgenie(resource_type='on_call')`
- List services: `query_opsgenie(resource_type='services')`

## RCA Investigation Workflow

**Step 1 -- Check for related open alerts:**
`query_opsgenie(resource_type='alerts', query='status=open', time_from='-6h')`

**Step 2 -- Get details on the triggering alert (logs, notes, timeline):**
`query_opsgenie(resource_type='alert_details', identifier='ALERT_ID')`

**Step 3 -- Check for correlated incidents:**
`query_opsgenie(resource_type='incidents', time_from='-24h')`

**Step 4 -- Identify affected services:**
`query_opsgenie(resource_type='services')`

**Step 5 -- Check who is on-call for escalation:**
`query_opsgenie(resource_type='on_call')`

**Step 6 -- Review team ownership:**
`query_opsgenie(resource_type='teams')`

## Important Rules
- Use ONLY the `query_opsgenie` tool. Do not attempt direct API calls.
- The `resource_type` parameter is required and must be one of: alerts, alert_details, incidents, incident_details, services, on_call, schedules, teams.
- Detail queries (`alert_details`, `incident_details`) require the `identifier` parameter.
- Time parameters accept relative strings (`'-1h'`, `'-24h'`) or ISO 8601 timestamps.
- Results are truncated at the output size limit. Use more specific queries to narrow results.
- OpsGenie alerts correlate with infrastructure issues -- check alert tags and descriptions for service names to focus cloud investigation.
