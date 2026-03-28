---
phase: 01-connection-management
plan: 01
subsystem: api
tags: [loki, grafana, http-client, requests, postgresql, observability]

# Dependency graph
requires: []
provides:
  - "LokiClient HTTP client class with bearer, basic, and no-auth modes"
  - "LokiAPIError custom exception for Loki API error handling"
  - "loki_alerts database table definition with JSONB labels/annotations"
  - "Loki API v1 method coverage: query_range, query, labels, label_values, series, index_stats, get_rules, get_alerts"
affects: [01-02, 02-alert-pipeline, 03-frontend]

# Tech tracking
tech-stack:
  added: []
  patterns: ["LokiClient follows BigPanda/Coroot requests.Session pattern", "Two-step test_connection: /ready then /loki/api/v1/labels"]

key-files:
  created:
    - server/connectors/loki_connector/__init__.py
    - server/connectors/loki_connector/client.py
  modified:
    - server/utils/db/db_utils.py

key-decisions:
  - "Used module-level LOKI_TIMEOUT constant (not class attribute) matching Grafana/BigPanda pattern"
  - "Added _request_raw helper for non-JSON endpoints (test_connection /ready returns plain text)"
  - "Denormalized labels and annotations as JSONB columns in loki_alerts for fast querying"

patterns-established:
  - "LokiClient: stateless HTTP client with requests.Session, no caching or retry logic"
  - "loki_alerts table: follows grafana_alerts/datadog_events pattern with Loki-specific fields (rule_group, labels JSONB, annotations JSONB)"

requirements-completed: [CONN-01, CONN-02, CONN-03, CONN-04, CONN-05]

# Metrics
duration: 2min
completed: 2026-03-28
---

# Phase 01 Plan 01: LokiClient HTTP Client and Database Table Summary

**LokiClient with three auth modes (bearer, basic, none), multi-tenant X-Scope-OrgID, two-step connection test, and full Loki API v1 query coverage plus loki_alerts table**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-28T21:46:56Z
- **Completed:** 2026-03-28T21:48:55Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- LokiClient class supporting bearer token, basic auth, and unauthenticated access with optional multi-tenant X-Scope-OrgID header
- Two-step test_connection: GET /ready (readiness probe) then GET /loki/api/v1/labels (credential validation)
- Complete Loki API v1 query method coverage: query_range, query, labels, label_values, series, index_stats, get_rules, get_alerts
- loki_alerts table definition with denormalized JSONB labels/annotations and three performance indexes

## Task Commits

Each task was committed atomically:

1. **Task 1: Create LokiClient HTTP client class** - `48043dbb` (feat)
2. **Task 2: Add loki_alerts table definition to db_utils.py** - `44181752` (feat)

## Files Created/Modified
- `server/connectors/loki_connector/__init__.py` - Package init re-exporting LokiClient and LokiAPIError
- `server/connectors/loki_connector/client.py` - LokiClient HTTP client with all auth modes and API methods (218 lines)
- `server/utils/db/db_utils.py` - Added loki_alerts table definition after grafana_alerts

## Decisions Made
- Used module-level LOKI_TIMEOUT = 30 constant (matching Grafana/BigPanda pattern) rather than class attribute
- Added _request_raw helper method for endpoints returning non-JSON (the /ready endpoint returns plain text "ready")
- Denormalized labels and annotations as separate JSONB columns in loki_alerts (Loki alerts are label-heavy, faster than extracting from payload)
- Placed loki_alerts table after grafana_alerts to keep monitoring connectors grouped together

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- LokiClient is ready for use by routes in Plan 02 (connect, status, disconnect, query endpoints)
- loki_alerts table will be created on next application startup when create_tables runs
- All auth modes verified via instantiation tests

## Self-Check: PASSED

All files exist, all commits verified.

---
*Phase: 01-connection-management*
*Completed: 2026-03-28*
