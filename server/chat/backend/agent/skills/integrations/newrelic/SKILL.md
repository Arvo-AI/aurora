---
name: newrelic
id: newrelic
description: "New Relic observability integration for running NRQL queries, checking alert issues, and searching entities via NerdGraph during RCA investigations"
category: observability
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.newrelic_tool
  function: is_newrelic_connected
tools:
  - query_newrelic
index: "Observability platform -- NRQL queries, alert issues, entity search via NerdGraph"
rca_priority: 3
allowed-tools: query_newrelic
metadata:
  author: aurora
  version: "1.0"
---

# New Relic Integration

## Overview
New Relic integration for querying observability data during Root Cause Analysis. New Relic is a REMOTE service. Use ONLY the `query_newrelic` API tool. All data is accessed via NerdGraph with `resource_type` parameter.

## Instructions

### Tool Usage
`query_newrelic(resource_type=TYPE, query=QUERY, time_range=RANGE, limit=N)`

### Resource Types
1. `'nrql'` -- Run NRQL queries. query=NRQL string e.g. `"SELECT count(*) FROM Transaction WHERE error IS true FACET appName"`
2. `'issues'` -- Active alert issues. query=state filter (`ACTIVATED`, `CREATED`, `CLOSED`)
3. `'entities'` -- Search monitored entities (APM apps, hosts). query=entity name or filter

### NRQL Tips
- Event types: `Transaction`, `TransactionError`, `SystemSample`, `Log`, `Span`, `ProcessSample`
- Use `FACET` for grouping results by field
- Use `TIMESERIES` for trends over time
- Use `SINCE` for time range (auto-injected from `time_range` if not present)
- Use `LIMIT` to cap results (auto-injected if not present)
- Only SELECT/FROM/WHERE/FACET queries are supported (no mutating operations)

### Common NRQL Queries
- Error count: `SELECT count(*) FROM TransactionError WHERE appName='X' SINCE 1 hour ago TIMESERIES`
- CPU usage: `SELECT average(cpuPercent) FROM SystemSample FACET hostname SINCE 30 minutes ago`
- Error logs: `SELECT count(*) FROM Log WHERE level = 'ERROR' FACET service SINCE 1 hour ago`
- Slow transactions: `SELECT average(duration) FROM Transaction WHERE appName='X' FACET name SINCE 1 hour ago`
- Throughput: `SELECT rate(count(*), 1 minute) FROM Transaction FACET appName SINCE 1 hour ago TIMESERIES`

### Entity Search
- Search by name: `query_newrelic(resource_type='entities', query='production-api')`
- Filter by type: `query_newrelic(resource_type='entities', query='production-api|APPLICATION')`
- Supported entity types: `APPLICATION`, `HOST`, `MONITOR`, `WORKLOAD`, `DASHBOARD`

### Examples
- NRQL: `query_newrelic(resource_type='nrql', query="SELECT count(*) FROM Transaction WHERE appName = 'my-app' SINCE 1 hour ago")`
- Issues: `query_newrelic(resource_type='issues', query='ACTIVATED')`
- Entities: `query_newrelic(resource_type='entities', query='production-api')`

## RCA Investigation Workflow

**Step 1 -- Check alert issues for active/related context:**
`query_newrelic(resource_type='issues', query='ACTIVATED')`

**Step 2 -- Search entities to identify affected services and hosts:**
`query_newrelic(resource_type='entities', query='affected-service')`

**Step 3 -- Use NRQL to query transactions and errors around the alert time:**
`query_newrelic(resource_type='nrql', query="SELECT count(*) FROM TransactionError FACET appName SINCE 1 hour ago TIMESERIES")`

**Step 4 -- Check infrastructure metrics:**
`query_newrelic(resource_type='nrql', query="SELECT average(cpuPercent), average(memoryUsedPercent) FROM SystemSample FACET hostname SINCE 1 hour ago")`

**Step 5 -- Correlate with logs:**
`query_newrelic(resource_type='nrql', query="SELECT count(*) FROM Log WHERE level = 'ERROR' FACET service, message SINCE 1 hour ago")`

## Important Rules
- New Relic is a REMOTE service. Use ONLY the `query_newrelic` API tool.
- The `resource_type` parameter is required and must be one of: nrql, issues, entities.
- Use `'nrql'` for any data query -- logs, metrics, transactions, errors, spans, infrastructure.
- NRQL queries must not contain mutating keywords (DROP, DELETE, INSERT, UPDATE, CREATE, ALTER).
- The `time_range` parameter (e.g., `'1 hour'`, `'30 minutes'`) is only used when the NRQL query does not already contain a `SINCE` clause.
- Results are truncated at the output size limit. Use `LIMIT` or more specific `WHERE` clauses to narrow results.
