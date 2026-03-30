---
phase: 04-query-capabilities
plan: 01
subsystem: server/routes/loki
tags: [logql, query, loki, api, routes]
dependency_graph:
  requires: [01-01, 01-02]
  provides: [query-range-route, instant-query-route, build-loki-client-helper]
  affects: [04-02]
tech_stack:
  added: []
  patterns: [credential-retrieval-pattern, client-builder-pattern, nanosecond-epoch-defaults]
key_files:
  created: []
  modified:
    - server/routes/loki/loki_routes.py
decisions:
  - _build_loki_client follows Datadog _build_client_from_creds pattern for consistency
  - Metric queries use same /logs/query_range endpoint with step param (no separate route)
  - Default time range is 1 hour (end=now, start=now-1h) using nanosecond epoch strings
  - Limit clamped to max 5000 to prevent excessive Loki load
metrics:
  duration: 2min
  completed: 2026-03-30
  tasks: 2
  files: 1
---

# Phase 04 Plan 01: LogQL Query Routes Summary

LogQL range and instant query routes with metric query support via step parameter, following Datadog search_logs pattern

## What Was Done

### Task 1: _build_loki_client helper and POST /logs/query_range route
**Commit:** `78ad31ec`

Added `_build_loki_client(creds)` helper function that constructs a `LokiClient` from Vault-stored credentials, following the Datadog `_build_client_from_creds` pattern. Added `POST /logs/query_range` route that accepts LogQL query, start/end time (nanosecond epoch strings), limit (clamped to 5000), direction, and optional `step` parameter. When `step` is present, Loki treats the query as a metric query (rate, count_over_time) and returns matrix results instead of streams (satisfying QUERY-05).

**Key details:**
- `from datetime import datetime, timezone` added for nanosecond epoch default calculation
- Default end = current UTC time as nanosecond epoch, default start = 1 hour before end
- Error handling: missing query (400), no connection (400), incomplete creds (400), LokiAPIError (502)

### Task 2: POST /logs/query route for instant queries
**Commit:** `6d49538e`

Added `POST /logs/query` route for point-in-time LogQL instant queries. Accepts query (required), optional `time` parameter (if absent, Loki uses server time), `limit` (clamped to 5000), and `direction`. Follows identical credential retrieval and client build pattern as query_range.

**Key details:**
- Uses `client.query()` method instead of `client.query_range()`
- Optional `time` param maps to Loki's instant query timestamp
- Same error handling pattern as query_range

## Requirements Covered

| Requirement | Description | Status |
|-------------|-------------|--------|
| QUERY-01 | LogQL range queries via query_range with time range and limit | Complete |
| QUERY-02 | LogQL instant queries via query | Complete |
| QUERY-05 | LogQL metric queries via query_range with step parameter | Complete |

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

All success criteria verified:
- `_build_loki_client`, `query_range`, `query_instant` functions present (AST-verified)
- All routes use `@require_permission("connectors", "read")` for RBAC
- All routes handle missing connection (400), incomplete creds (400), Loki API errors (502)
- `query_range` accepts optional `step` parameter for metric queries
- File parses without syntax errors

## Self-Check: PASSED

- server/routes/loki/loki_routes.py: FOUND
- .planning/phases/04-query-capabilities/04-01-SUMMARY.md: FOUND
- Commit 78ad31ec: FOUND
- Commit 6d49538e: FOUND
