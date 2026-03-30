---
phase: 03-frontend-integration
plan: 01
subsystem: ui
tags: [nextjs, typescript, api-routes, connector-registry, loki]

# Dependency graph
requires:
  - phase: 01-connection-management
    provides: Backend Loki connection routes (/loki/connect, /loki/status, /loki/disconnect)
  - phase: 02-alert-pipeline
    provides: Backend Loki alert routes (/loki/alerts/webhook-url)
provides:
  - Loki service layer (lokiService) with getStatus, connect, getWebhookUrl methods
  - API route proxies for /api/loki/connect, /api/loki/status, /api/loki/alerts/webhook-url
  - ConnectorRegistry entry for Loki under Monitoring category
  - Disconnect wiring in connected-accounts route
  - Loki SVG icon asset
affects: [03-02-frontend-integration]

# Tech tracking
tech-stack:
  added: []
  patterns: [loki-service-pattern, loki-api-proxy-pattern]

key-files:
  created:
    - client/src/lib/services/loki.ts
    - client/src/app/api/loki/connect/route.ts
    - client/src/app/api/loki/status/route.ts
    - client/src/app/api/loki/alerts/webhook-url/route.ts
    - client/public/loki.svg
  modified:
    - client/src/components/connectors/ConnectorRegistry.ts
    - client/src/app/api/connected-accounts/[provider]/route.ts

key-decisions:
  - "Followed Netdata connector pattern exactly for service layer and API proxies"
  - "Loki service maps both camelCase and snake_case response fields for backend compatibility"

patterns-established:
  - "Loki service layer: apiRequest-based client with camelCase/snake_case response mapping"
  - "Loki API proxies: getAuthenticatedUser + BACKEND_URL fetch pattern matching Netdata"

requirements-completed: [FE-01, FE-04]

# Metrics
duration: 3min
completed: 2026-03-30
---

# Phase 3 Plan 1: Frontend Plumbing Summary

**Loki ConnectorRegistry entry, service layer with 3 API methods, 3 proxy routes, disconnect wiring, and SVG icon**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-30T18:33:13Z
- **Completed:** 2026-03-30T18:36:18Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- Created Loki service layer with getStatus, connect, getWebhookUrl methods following Netdata pattern
- Built 3 API route proxies (connect POST, status GET, webhook-url GET) with auth and error handling
- Registered Loki in ConnectorRegistry under Monitoring with alertsPath and storageKey
- Wired disconnect in connected-accounts route with DELETE to /loki/disconnect
- Added loki.svg icon with amber gradient

## Task Commits

Each task was committed atomically:

1. **Task 1: Create Loki service layer and API route proxies** - `ee2848f4` (feat)
2. **Task 2: Register Loki in ConnectorRegistry, add disconnect wiring, add icon** - `82ea4492` (feat)

## Files Created/Modified
- `client/src/lib/services/loki.ts` - Service layer with LokiStatus, LokiConnectPayload, WebhookUrlResponse interfaces and lokiService object
- `client/src/app/api/loki/connect/route.ts` - POST proxy to backend /loki/connect with auth headers
- `client/src/app/api/loki/status/route.ts` - GET proxy to backend /loki/status with cache control
- `client/src/app/api/loki/alerts/webhook-url/route.ts` - GET proxy to backend /loki/alerts/webhook-url
- `client/public/loki.svg` - Amber gradient icon with L letterform
- `client/src/components/connectors/ConnectorRegistry.ts` - Added Loki registration under Monitoring
- `client/src/app/api/connected-accounts/[provider]/route.ts` - Added loki to whitelist and disconnect handler

## Decisions Made
- Followed Netdata connector pattern exactly for consistency across all monitoring connectors
- Service layer maps both camelCase and snake_case response fields (baseUrl/base_url, authType/auth_type, tenantId/tenant_id) to handle backend response format variations

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- node_modules not installed in worktree, preventing local TypeScript compilation check. Verified against main repo's TypeScript setup instead -- no Loki-related errors found.

## User Setup Required

None - no external service configuration required.

## Known Stubs

None - all service methods, API routes, registry entries, and disconnect handlers are fully wired.

## Next Phase Readiness
- All frontend plumbing complete for Plan 02 (auth page UI)
- lokiService is importable from @/lib/services/loki
- API routes are live at /api/loki/* paths
- ConnectorRegistry entry will render Loki in the connectors grid
- Disconnect wiring will work when the auth page calls the disconnect flow

## Self-Check: PASSED

- All 7 files verified present on disk
- Both task commits (ee2848f4, 82ea4492) verified in git log

---
*Phase: 03-frontend-integration*
*Completed: 2026-03-30*
