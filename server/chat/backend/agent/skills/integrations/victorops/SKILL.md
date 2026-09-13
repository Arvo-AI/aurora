---
name: victorops
id: victorops
description: "Splunk On-Call (VictorOps) integration for querying incident history, on-call schedules, and team context during RCA investigations"
category: incident_management
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.victorops_tool
  function: is_victorops_connected
tools:
  - get_victorops_incidents
  - get_victorops_teams
index: "On-call incident management -- query recent incidents, team rosters, and on-call schedules from Splunk On-Call"
rca_priority: 4
allowed-tools: get_victorops_incidents, get_victorops_teams
metadata:
  author: aurora
  version: "1.0"
---

# Splunk On-Call Integration

## Overview
Splunk On-Call (formerly VictorOps) is an on-call incident management platform. Use this integration during RCA to retrieve incident history, check which teams are on-call, and correlate the current alert with past incidents. This is a REMOTE API — do NOT search the local filesystem.

## Instructions

### Tool Usage
1. `get_victorops_incidents()` — Retrieve recent incidents from Splunk On-Call. Use during RCA to find similar past incidents or check incident history.
2. `get_victorops_teams()` — List teams and on-call schedules. Use to identify who was on-call when the incident triggered.

### RCA Workflow
- **Read-only**: Only query incident data during RCA. Never create, acknowledge, or resolve incidents automatically.
- Use `get_victorops_incidents` to find related past incidents and identify patterns.
- Cross-reference incident timeline with metrics from connected monitoring tools (Datadog, Grafana, etc.).
- Note the routing key and escalation path to understand which service or team is responsible.

### Context to gather
- Recent incidents for the same service/routing key
- Incident frequency and recurring patterns
- Team ownership and on-call rotation at time of incident
