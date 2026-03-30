---
phase: 02-alert-pipeline
plan: 02
subsystem: api
tags: [celery, flask, webhook, alertmanager, grafana, loki, postgres, sse, rca]

# Dependency graph
requires:
  - phase: 01-connection-management
    provides: LokiClient, connect/status/disconnect routes, Vault credential storage
  - phase: 02-alert-pipeline (plan 01)
    provides: helpers.py (normalize, dedup hash, severity/service extraction), rca_prompt_builder, loki_alerts schema
provides:
  - Celery task process_loki_alert for multi-alert batch processing
  - Webhook endpoint POST /loki/alerts/webhook/<user_id>
  - Alert listing endpoint GET /loki/alerts with pagination and state filter
  - Webhook URL endpoint GET /loki/alerts/webhook-url with setup instructions
affects: [03-frontend-integration, alert-correlation, incident-management]

# Tech tracking
tech-stack:
  added: []
  patterns: [multi-alert-batch-processing, loki-webhook-pipeline]

key-files:
  created:
    - server/routes/loki/tasks.py
  modified:
    - server/routes/loki/loki_routes.py

key-decisions:
  - "Iterate over normalized_alerts list to handle Alertmanager v4 multi-alert batches independently"
  - "Webhook route is unauthenticated (user_id in URL) matching Grafana/Netdata/Datadog pattern"
  - "Provide instructions for both Alertmanager receiver and Grafana Alerting contact point setup"

patterns-established:
  - "Loki alert pipeline: webhook -> Celery -> normalize -> hash dedup -> store -> correlate -> incident -> SSE -> summary -> RCA"
  - "Multi-alert batch: each alert in payload.alerts[] processed independently with its own incident and RCA"

requirements-completed: [ALERT-01, ALERT-03, ALERT-04, ALERT-05]

# Metrics
duration: 4min
completed: 2026-03-30
---

# Phase 2 Plan 2: Alert Pipeline Routes Summary

**Celery task and Flask routes for Loki webhook alert processing with multi-alert batch support, dedup, incident creation, and automated RCA**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-30T18:15:17Z
- **Completed:** 2026-03-30T18:19:24Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Created Celery task process_loki_alert that handles multi-alert batches from Alertmanager v4 and Grafana Unified formats
- Full pipeline per alert: normalize -> hash dedup -> store in loki_alerts -> AlertCorrelator -> create incident -> SSE broadcast -> summary generation -> background RCA with Loki-specific prompt
- Added webhook receiver route at POST /loki/alerts/webhook/<user_id> that validates connection and dispatches to Celery
- Added alert listing route at GET /loki/alerts with pagination (limit/offset) and state filter
- Added webhook URL generation route at GET /loki/alerts/webhook-url with dual setup instructions (Alertmanager + Grafana)

## Task Commits

Each task was committed atomically:

1. **Task 1: Create Celery task for Loki alert processing** - `c6e1f8be` (feat)
2. **Task 2: Add webhook, alert listing, and webhook-url routes** - `18eee2ee` (feat)

## Files Created/Modified
- `server/routes/loki/tasks.py` - Celery task process_loki_alert: normalizes dual-format webhooks, stores with dedup hash, correlates alerts, creates incidents, broadcasts SSE, triggers summary and RCA
- `server/routes/loki/loki_routes.py` - Added 3 new routes: webhook receiver, alert listing with pagination, webhook URL generator with Alertmanager + Grafana setup instructions

## Decisions Made
- Iterate over normalized_alerts list to handle multi-alert batches independently (each alert gets its own incident and RCA)
- Webhook route is unauthenticated (user_id in URL) matching the established pattern from Grafana/Netdata/Datadog connectors
- Provide instructions for both Alertmanager receiver configuration and Grafana Alerting contact point setup in webhook-url response

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Full backend alert pipeline is complete (Phase 2 done)
- Ready for Phase 3: Frontend Integration (ConnectorRegistry, auth page, webhook URL display)
- Frontend can proxy to all backend routes: connect, status, disconnect, alerts, webhook-url

## Self-Check: PASSED

- FOUND: server/routes/loki/tasks.py
- FOUND: server/routes/loki/loki_routes.py
- FOUND: .planning/phases/02-alert-pipeline/02-02-SUMMARY.md
- FOUND: c6e1f8be (Task 1 commit)
- FOUND: 18eee2ee (Task 2 commit)

---
*Phase: 02-alert-pipeline*
*Completed: 2026-03-30*
