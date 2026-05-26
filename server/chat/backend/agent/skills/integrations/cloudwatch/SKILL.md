---
name: cloudwatch
id: cloudwatch
description: "Amazon CloudWatch alarm integration — receives alarm state-change notifications via SNS webhooks and creates incidents automatically"
category: observability
connection_check:
  method: provider_in_preference
  provider_key: cloudwatch
tools: []
index: "CloudWatch — observation-only alarm ingestion via SNS webhook, no CLI tools"
rca_priority: 30
allowed-tools: ""
metadata:
  author: aurora
  version: "1.0"
---

# CloudWatch Integration

## Overview

Amazon CloudWatch alarm integration. Aurora receives alarm state-change notifications from CloudWatch via AWS SNS webhooks and creates incidents automatically.

## Instructions

### IMPORTANT — NO CLI SUPPORT

- Do NOT use `cloud_exec('cloudwatch', ...)` — there is no CloudWatch CLI connector.
- You CAN use `cloud_exec('aws', ...)` to query CloudWatch metrics and logs via the AWS CLI.

### WHAT YOU CAN DO

- **Investigate infrastructure**: If an alarm references a specific AWS resource (EC2, RDS, Lambda, EKS),
  use the AWS cloud provider tool (`cloud_exec` with 'aws') to investigate.

### CRITICAL RULES

- NEVER call cloud_exec with provider='cloudwatch' — it will fail.
- Use the alert context already available in the conversation.
- For deeper investigation, use the AWS provider tools for the affected resource.

## RCA Investigation Workflow (read-only)

During RCA the agent may:
- Query CloudWatch alarms history for the affected account/region
- Cross-reference with EC2, RDS, Lambda, EKS metrics for the affected resource
- Look up related incidents with the same alarm_name
- Check AWS CloudWatch Logs for errors near the alarm fire time

The agent must never write or modify CloudWatch alarms during RCA.

## Alarm State Mapping

| CloudWatch State | Aurora Behavior |
|---|---|
| ALARM | Creates incident — severity **high** (or **critical** if "critical" in alarm name) |
| INSUFFICIENT_DATA | No incident created (state recorded only) |
| OK | Resolves matching open incident (no new incident created) |
