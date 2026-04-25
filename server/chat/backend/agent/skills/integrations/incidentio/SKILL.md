---
name: incidentio
id: incidentio
description: "incident.io integration for listing incidents, investigating details, and reviewing timelines during RCA"
category: incident_management
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.incidentio_tool
  function: is_incidentio_connected
tools:
  - list_incidentio_incidents
  - get_incidentio_incident
  - get_incidentio_timeline
index: "Incident management -- list incidents, get details, review timeline"
rca_priority: 2
allowed-tools: list_incidentio_incidents, get_incidentio_incident, get_incidentio_timeline
metadata:
  author: aurora
  version: "1.0"
---

# incident.io Integration

## Overview
incident.io integration for investigating incidents during Root Cause Analysis. Provides access to incident details, severity, roles, custom fields, and timeline events.

## Instructions

### Tool Usage (use in this order)
1. `list_incidentio_incidents()` -- Find recent or related incidents. Filter by status or severity.
2. `get_incidentio_incident(incident_id='X')` -- Get full details including roles, custom fields, and duration.
3. `get_incidentio_timeline(incident_id='X')` -- See the sequence of events, status changes, and human updates.

### Investigation Patterns
- Find similar incidents: `list_incidentio_incidents(status='closed', page_size=10)` to check for recurring issues.
- Deep-dive current incident: `get_incidentio_incident(incident_id='...')` for severity, responders, and custom fields.
- Understand timeline: `get_incidentio_timeline(incident_id='...')` to see what actions were taken and when.

## RCA Investigation Workflow

**Step 1 -- Context gathering:**
`list_incidentio_incidents(status='live')` -- See what's currently happening.

**Step 2 -- Incident details:**
`get_incidentio_incident(incident_id='...')` -- Understand severity, who's responding, and what services are affected.

**Step 3 -- Timeline analysis:**
`get_incidentio_timeline(incident_id='...')` -- Reconstruct the sequence of events to find the trigger point.

**Step 4 -- Pattern matching:**
`list_incidentio_incidents(status='closed', page_size=25)` -- Check if similar incidents happened before.

**Step 5 -- Cross-correlate:**
After incident.io analysis, correlate with infrastructure data from other connected tools (logs, metrics, deployments).

## Important Rules
- incident.io is a REMOTE service. Use only the API tools listed above.
- Start with `list_incidentio_incidents` to understand the landscape before diving into specifics.
- Timeline data is essential for understanding causality -- always check it during RCA.
- Incident.io roles tell you who was involved, which helps validate findings.
- Custom fields often contain service/component info useful for correlation.
