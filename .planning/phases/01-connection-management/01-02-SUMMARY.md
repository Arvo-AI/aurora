---
phase: 01-connection-management
plan: 02
subsystem: api
tags: [flask, loki, vault, rbac, connector]

# Dependency graph
requires:
  - phase: 01-connection-management/01-01
    provides: LokiClient HTTP client with test_connection(), LokiAPIError exception
provides:
  - Loki registered in SUPPORTED_SECRET_PROVIDERS for Vault credential storage
  - store_tokens_in_db elif branch for Loki with base_url/auth_type metadata
  - Flask Blueprint with POST /loki/connect, GET /loki/status, POST|DELETE /loki/disconnect
  - Blueprint registered in main_compute.py at /loki prefix
affects: [02-alert-pipeline, 03-frontend-integration]

# Tech tracking
tech-stack:
  added: []
  patterns: [multi-auth connector routes (bearer/basic/none), URL normalization accepting both HTTP and HTTPS]

key-files:
  created:
    - server/routes/loki/__init__.py
    - server/routes/loki/loki_routes.py
  modified:
    - server/utils/secrets/secret_ref_utils.py
    - server/utils/auth/token_management.py
    - server/main_compute.py

key-decisions:
  - "Store Loki base_url as client_id and auth_type as subscription_name in user_tokens table, matching Spinnaker pattern"
  - "URL normalization accepts both HTTP and HTTPS schemes since Loki is often internal/HTTP"

patterns-established:
  - "Multi-auth connector: single connect route handling bearer, basic, and no-auth modes via authType field"
  - "Optional tenant_id for multi-tenant Loki deployments stored alongside credentials in Vault"

requirements-completed: [CONN-01, CONN-02, CONN-03, CONN-04, CONN-05, CONN-06, CONN-07]

# Metrics
duration: 2min
completed: 2026-03-28
---

# Phase 01 Plan 02: Loki Connection Routes Summary

**Flask routes for Loki connect/status/disconnect with bearer, basic, and no-auth modes plus Vault credential storage via registered provider**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-28T21:51:45Z
- **Completed:** 2026-03-28T21:54:11Z
- **Tasks:** 3
- **Files modified:** 5

## Accomplishments
- Registered "loki" in SUPPORTED_SECRET_PROVIDERS so get_token_data returns Loki credentials (fixes Research Pitfall 4)
- Added store_tokens_in_db elif branch storing base_url and auth_type metadata (fixes Research Pitfall 5)
- Created three Flask routes: connect (with two-step validation), status (returns connection metadata), disconnect (removes Vault credentials)
- Blueprint registered in main_compute.py at /loki prefix between Grafana and Datadog

## Task Commits

Each task was committed atomically:

1. **Task 1: Register Loki as a supported secret provider** - `8a727268` (feat)
2. **Task 2: Create Flask routes for Loki connection management** - `6d8ea0eb` (feat)
3. **Task 3: Register Loki blueprint in main_compute.py** - `ce4af8e1` (feat)

## Files Created/Modified
- `server/utils/secrets/secret_ref_utils.py` - Added "loki" to SUPPORTED_SECRET_PROVIDERS set
- `server/utils/auth/token_management.py` - Added elif provider == "loki" branch in store_tokens_in_db
- `server/routes/loki/__init__.py` - Package init re-exporting loki_bp as bp
- `server/routes/loki/loki_routes.py` - Flask Blueprint with connect, status, disconnect routes
- `server/main_compute.py` - Blueprint registration at /loki URL prefix

## Decisions Made
- Store Loki base_url as client_id and auth_type as subscription_name in user_tokens table, matching the Spinnaker pattern for metadata storage
- URL normalization accepts both HTTP and HTTPS since Loki is frequently deployed on internal networks without TLS

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Connection management routes are complete and ready for Phase 2 (Alert Pipeline)
- The connect route validates credentials via LokiClient.test_connection() and stores them in Vault
- Phase 2 will add webhook alert routes, Celery tasks, and routes.loki.tasks module
- Phase 3 (Frontend) can now build against /loki/connect, /loki/status, /loki/disconnect endpoints

## Self-Check: PASSED

All 5 created/modified files verified on disk. All 3 task commits verified in git log.

---
*Phase: 01-connection-management*
*Completed: 2026-03-28*
