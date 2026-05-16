---
name: sentry
id: sentry
description: "Sentry error tracking integration for searching issues, fetching stacktraces, listing projects, and running Discover event searches during RCA investigations"
category: observability
connection_check:
  method: get_token_data
  provider_key: sentry
  required_field: auth_token
tools:
  - query_sentry
index: "Error tracking platform -- issue search, full stacktraces, breadcrumbs, project listings, Discover event queries"
rca_priority: 3
allowed-tools: query_sentry
metadata:
  author: aurora
  version: "1.0"
---

# Sentry Integration

## Overview
Sentry error tracking integration for querying issues, events, and project data during Root Cause Analysis. Sentry is a REMOTE service. Use ONLY the `query_sentry` API tool. All data is accessed via the Sentry web API with the `resource_type` parameter.

## Instructions

### Tool Usage
`query_sentry(resource_type=TYPE, query=QUERY, stats_period=PERIOD, project=PROJECT, limit=N)`

### Resource Types
1. `'issues'` -- Search issues. `query` is a Sentry search expression (e.g. `is:unresolved level:error`)
2. `'issue_detail'` -- Get metadata for one issue. `query` is the numeric Sentry issue ID
3. `'issue_event'` -- Get the latest event for an issue with full stacktrace + breadcrumbs + tags. `query` is the issue ID
4. `'projects'` -- List projects in the connected Sentry org
5. `'events'` -- Discover-style event table search. `query` is a Sentry search expression

### Sentry Search Syntax
- `key:value` -- exact match (e.g. `level:error`, `environment:production`)
- `AND` / `OR` -- combine clauses (e.g. `level:error AND environment:production`)
- `!key:value` -- negation
- `key:[a,b]` -- value in list
- `>`, `<`, `>=`, `<=` -- comparison on numeric/date fields
- `*` -- wildcard
- Common keys: `level`, `environment`, `release`, `project`, `is`, `assigned`, `bookmarks`, `has`

### stats_period
- Format: `Nm` minutes, `Nh` hours, `Nd` days, `Nw` weeks (e.g. `24h`, `7d`, `30m`)
- Defaults to `24h` if omitted

### Common Searches
- Unresolved errors: `query_sentry(resource_type='issues', query='is:unresolved level:error', stats_period='24h')`
- Production fatal errors: `query_sentry(resource_type='issues', query='level:fatal environment:production', stats_period='1h')`
- Errors in a specific release: `query_sentry(resource_type='issues', query='release:v1.2.3 is:unresolved')`
- Issue details: `query_sentry(resource_type='issue_detail', query='1234567890')`
- Full stacktrace for an issue: `query_sentry(resource_type='issue_event', query='1234567890')`
- All projects: `query_sentry(resource_type='projects')`
- Events from a user: `query_sentry(resource_type='events', query='user.email:foo@example.com', stats_period='6h')`

## RCA Investigation Workflow

**Step 1 -- Find the issue triggered by the alert:**
If the alert webhook included an `issueId`, fetch the issue directly:
`query_sentry(resource_type='issue_detail', query='ISSUE_ID')`

Otherwise search recent unresolved issues:
`query_sentry(resource_type='issues', query='is:unresolved level:error', stats_period='1h')`

**Step 2 -- Fetch the full stacktrace and breadcrumbs:**
`query_sentry(resource_type='issue_event', query='ISSUE_ID')`

The response includes `entries[].data.values[].stacktrace.frames`, breadcrumbs, tags, user, release, and environment -- everything needed to identify the failing code path.

**Step 3 -- Check the project context:**
`query_sentry(resource_type='projects')`

Use this to map the Sentry project slug to a code repository or service.

**Step 4 -- Look for related events in the same time window:**
`query_sentry(resource_type='events', query='project:PROJECT_SLUG environment:production', stats_period='1h')`

This surfaces clustering -- whether the error coincides with other issues from the same release, environment, or user.

**Step 5 -- Check if the issue is a regression:**
Look at the issue's `firstSeen` and `lastSeen` in the `issue_detail` response. If `firstSeen` is recent and the affected `release` differs from the previous stable release, it's likely a regression.

## Important Rules
- Sentry is a REMOTE service. Use ONLY the `query_sentry` API tool.
- The `resource_type` parameter is required and must be one of: issues, issue_detail, issue_event, projects, events.
- For `issue_detail` and `issue_event`, the `query` parameter MUST be the numeric Sentry issue ID.
- Sentry tokens are read-only in Aurora -- never call mutating endpoints during RCA.
- Results are truncated at the output size limit. Use a more specific `query` (e.g. add `level:error`, `environment:production`, or a `release:` filter) to narrow results when truncated.
- `issue_event` payloads can be very large because they contain full stacktraces. Prefer `issue_detail` first; fetch the event only when you need the stacktrace.
