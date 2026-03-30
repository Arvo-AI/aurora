---
phase: 02-alert-pipeline
plan: 01
subsystem: api
tags: [loki, alertmanager, grafana, webhook, normalization, dedup, rca, flask]

# Dependency graph
requires:
  - phase: 01-connection-management
    provides: Loki client, connection routes, credential storage via user_tokens
provides:
  - Dual-format webhook normalization (Alertmanager v4 + Grafana Unified)
  - SHA-256 dedup hashing with fingerprint preference
  - Severity/service label extraction utilities
  - build_loki_rca_prompt for automated root cause analysis
  - Loki branch in incident source URL resolution
  - Loki branch in incident summary generation
  - loki_alerts table with org_id and alert_hash columns
affects: [02-alert-pipeline plan 02, frontend alerts page]

# Tech tracking
tech-stack:
  added: []
  patterns: [dual-format webhook detection, label-based severity/service extraction]

key-files:
  created:
    - server/routes/loki/helpers.py
  modified:
    - server/utils/db/db_utils.py
    - server/routes/incidents_routes.py
    - server/chat/background/summarization.py
    - server/chat/background/rca_prompt_builder.py

key-decisions:
  - "Default unknown webhook format to alertmanager_v4 since both formats share alerts[] structure"
  - "Use fingerprint-based hash when available, fall back to alert_name+service composite key"
  - "Loki severity mapping: critical->critical, warning->high, info->low, other->raw value"

patterns-established:
  - "Dual-format detection: check version==4 for Alertmanager, orgId presence for Grafana Unified"
  - "Grafana-specific fields (silenceURL, dashboardURL, panelURL, values) are None for Alertmanager format"

requirements-completed: [ALERT-02, ALERT-03, ALERT-06]

# Metrics
duration: 3min
completed: 2026-03-30
---

# Phase 02 Plan 01: Alert Pipeline Utilities Summary

**Dual-format Loki webhook normalization (Alertmanager v4 + Grafana Unified), SHA-256 dedup hashing, and integration hooks for RCA, incidents, and summarization**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-30T18:09:13Z
- **Completed:** 2026-03-30T18:12:29Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Created helpers.py with 7 exported functions: detect_webhook_format, normalize_loki_webhook, extract_severity, extract_service, generate_alert_hash, format_alert_summary, should_trigger_background_chat
- Added org_id and alert_hash columns to loki_alerts table, registered in RLS and org_id migration lists
- Integrated Loki into incident source URL resolution, summary generation, and RCA prompt building

## Task Commits

Each task was committed atomically:

1. **Task 1: Create Loki webhook helpers with dual-format normalization and dedup** - `4c8352dc` (feat)
2. **Task 2: Add loki_alerts schema fixes and integration hooks** - `783ff555` (feat)

## Files Created/Modified
- `server/routes/loki/helpers.py` - Webhook normalization, format detection, dedup hash, severity/service extraction, summary formatting
- `server/utils/db/db_utils.py` - Added org_id + alert_hash to loki_alerts table; registered in RLS and org_id migration
- `server/routes/incidents_routes.py` - Added Loki branch in _build_source_url to resolve base_url from credentials
- `server/chat/background/summarization.py` - Added Loki branch in _build_summary_prompt extracting labels/annotations
- `server/chat/background/rca_prompt_builder.py` - Added build_loki_rca_prompt delegating to shared build_rca_prompt

## Decisions Made
- Default unknown webhook format to alertmanager_v4 since both formats share alerts[] structure and Alertmanager is the more common path for Loki Ruler
- Use fingerprint-based hash when available, fall back to alert_name+service composite key for dedup
- Severity mapping: critical->critical, warning->high, info->low to match Aurora standard levels

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All utility functions and integration hooks ready for Plan 02 (Celery task + routes)
- Plan 02 can directly import: normalize_loki_webhook, generate_alert_hash, format_alert_summary, should_trigger_background_chat, build_loki_rca_prompt
- loki_alerts table schema ready with dedup support via alert_hash UNIQUE constraint

## Self-Check: PASSED

All 5 files verified present. Both commit hashes (4c8352dc, 783ff555) found. 7 exported functions in helpers.py confirmed.

---
*Phase: 02-alert-pipeline*
*Completed: 2026-03-30*
