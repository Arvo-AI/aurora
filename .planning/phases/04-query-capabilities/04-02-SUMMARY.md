---
phase: 04-query-capabilities
plan: 02
subsystem: server/routes/loki
tags: [loki, labels, series, discovery, api, routes]
dependency_graph:
  requires: [04-01]
  provides: [labels-route, label-values-route, series-route]
  affects: []
tech_stack:
  added: []
  patterns: [flask-blueprint-routes, vault-credential-retrieval, loki-api-proxy]
key_files:
  created: []
  modified:
    - server/routes/loki/loki_routes.py
decisions:
  - Label discovery routes use GET (read-only, no body); series uses POST (complex match parameter)
metrics:
  duration: 1min
  completed: "2026-03-30"
---

# Phase 04 Plan 02: Label and Series Discovery Routes Summary

Three discovery endpoints added to loki_routes.py: GET /labels lists stream label names, GET /label/{name}/values lists values for a label, POST /series finds streams matching label selectors.

## What Was Done

### Task 1: Add GET /labels and GET /label/{name}/values routes
- **Commit:** 280e1c63
- Added `list_labels` route at GET /labels returning all known stream label names
- Added `label_values` route at GET /label/<name>/values returning values for a specific label
- Both accept optional `start` and `end` query params for time-scoped discovery
- Both follow the established credential retrieval and error handling pattern (400 for missing connection/incomplete creds, 502 for Loki API errors)

### Task 2: Add POST /series route for stream discovery
- **Commit:** a70e083a
- Added `list_series` route at POST /series accepting label matchers (string or list) with optional time range
- Returns matching stream label sets for exploring available log streams
- Validates `match` field is present (400 if missing)
- Uses POST because match parameter can contain complex selectors with special characters

## Deviations from Plan

None - plan executed exactly as written.

## Verification Results

- All 3 discovery functions present: `list_labels`, `label_values`, `list_series`
- All routes use `@require_permission("connectors", "read")` for RBAC
- All routes handle missing connection (400), incomplete creds (400), and Loki API errors (502)
- File parses without syntax errors (AST parse verified)
- All 11 public route functions now present in loki_routes.py (6 from Phase 4)

## Known Stubs

None. All routes are fully wired to LokiClient methods.

## Decisions Made

| Decision | Rationale |
|----------|-----------|
| Label routes use GET, series uses POST | Labels/values are simple read-only lookups; series match parameter can be complex with special characters |

## Self-Check: PASSED

- FOUND: server/routes/loki/loki_routes.py
- FOUND: .planning/phases/04-query-capabilities/04-02-SUMMARY.md
- FOUND: commit 280e1c63
- FOUND: commit a70e083a
