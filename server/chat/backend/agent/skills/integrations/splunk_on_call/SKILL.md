---
name: splunk-on-call
id: splunk_on_call
description: "Splunk On-Call incident integration for read-only RCA context"
category: incident_management
connection_check:
  method: get_token_data
  provider_key: splunk_on_call
  required_field: api_key
tools:
  - query_splunk_on_call
index: "Incident management -- search Splunk On-Call incidents by phase and routing key"
rca_priority: 2
allowed-tools: query_splunk_on_call
metadata:
  author: aurora
  version: "1.0"
---

# Splunk On-Call Integration

Use `query_splunk_on_call` to inspect related incidents while performing RCA.

## RCA workflow

This integration is read-only during RCA.

1. Search `phase="unacked"` for active incidents.
2. Narrow by `routing_key_contains` when the triggering alert has a routing key.
3. Compare service, host, entity, and transition timestamps with other evidence.

Never acknowledge, resolve, reroute, or otherwise mutate Splunk On-Call incidents.
