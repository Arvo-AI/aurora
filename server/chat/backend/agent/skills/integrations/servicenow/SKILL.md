---
name: servicenow
id: servicenow
description: "ServiceNow ITSM integration for listing incidents and reading incident details during RCA"
category: incident_management
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.servicenow_tool
  function: is_servicenow_connected
tools:
  - list_servicenow_incidents
  - get_servicenow_incident
index: "Incident management -- list ServiceNow incidents, get incident details"
rca_priority: 2
allowed-tools: list_servicenow_incidents, get_servicenow_incident
metadata:
  author: aurora
  version: "1.0"
---

# ServiceNow Integration

## Overview
Read-only access to the customer's ServiceNow incident table for Root Cause Analysis: the incident that triggered the investigation, related incidents on the same configuration item or assignment group, and recent history of the same symptom.

## Instructions

### Tool Usage (use in this order)
1. `get_servicenow_incident(incident='INC0010001')` -- Full record for the incident under investigation (priority, impact, urgency, state, category, assignment group, configuration item, caller, description, close notes).
2. `list_servicenow_incidents(query='<symptom keyword>', limit=25)` -- Earlier incidents with the same symptom; close notes often hold the previous fix.
3. `list_servicenow_incidents(assignment_group='<group>', state='active')` -- What else the same team is dealing with right now.
4. `list_servicenow_incidents(priority=1, state='active')` -- Other critical incidents that may share a cause.

### Investigation Patterns
- The RCA prompt carries the webhook payload (number, sys_id, priority, state, configuration item). Use `get_servicenow_incident` with that number to read the full description and any close notes.
- Recurrence check: search by the configuration item name or a distinctive phrase from the short description with `state='resolved'` or `state='closed'`.
- Blast radius: list active incidents for the same assignment group or configuration item.

## Important Rules
- ServiceNow is a REMOTE service. Use only the tools listed above.
- Both tools are read-only. Aurora posts its RCA back as a work note through the connector, not through these tools.
- State values: 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled. Priority 1 is the most urgent.
- Results are newest first and capped at 100 per call; narrow with filters instead of paging.
