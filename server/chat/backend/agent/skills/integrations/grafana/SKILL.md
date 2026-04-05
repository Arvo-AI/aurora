---
name: grafana
id: grafana
description: "Grafana integration for alert ingestion and dashboard monitoring via Aurora's internal webhook pipeline"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools: []
index: "Grafana — observation-only alert ingestion, no CLI tools"
rca_priority: 10
allowed-tools: ""
metadata:
  author: aurora
  version: "1.0"
---

# Grafana Integration

## Overview
Grafana is connected as an **observation-only** provider for alert ingestion and dashboard monitoring.

## Instructions

### IMPORTANT -- NO CLI SUPPORT
- Do NOT use `cloud_exec('grafana', ...)` -- there is no Grafana CLI connector.
- Do NOT use `terminal_exec` with `grafana-cli` -- it is not installed.
- Grafana data (alerts) is available through Aurora's internal API, not through CLI tools.

### WHAT YOU CAN DO
- **View alerts**: Grafana alerts are automatically ingested via webhook and stored in Aurora's database.
  Reference the alert context provided in the conversation to answer questions about Grafana alerts.
- **Investigate infrastructure**: If an alert references a specific cloud resource (VM, pod, service),
  use the appropriate cloud provider tool (cloud_exec with 'gcp', 'aws', 'azure', etc.) to investigate.

### CRITICAL RULES
- NEVER call cloud_exec with provider='grafana' -- it will fail.
- Use the alert context already available in the conversation.
- For deeper investigation, identify the underlying cloud provider from the alert and use that provider's tools.
