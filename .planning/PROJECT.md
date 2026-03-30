# Grafana Loki Connector

## What This Is

A new connector for the Aurora platform that integrates Grafana Loki for centralized log aggregation. Users can connect their Loki instance, query logs via LogQL, receive alert webhooks that create incidents, and trigger automated root cause analysis — following the same patterns as existing Aurora connectors (Datadog, Netdata, Splunk).

## Core Value

Users can connect their Loki instance and receive alert-driven incidents with automated RCA, closing the loop from log alert to investigation.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] User can connect a Loki instance via URL + API token or basic auth credentials
- [ ] User can check Loki connection status and see instance metadata
- [ ] User can disconnect Loki and have all stored credentials removed
- [ ] User can query logs via LogQL (range queries and instant queries)
- [ ] User can list stream labels and label values for query building
- [ ] User can receive Loki Ruler/Alertmanager webhook alerts
- [ ] Webhook alerts are stored in the database with full payload
- [ ] Webhook alerts create incidents and trigger alert correlation
- [ ] Webhook alerts trigger automated RCA via background chat
- [ ] User can view received Loki alerts in a dedicated alerts page
- [ ] User can query log-derived metrics via LogQL metric queries
- [ ] Connector is registered in the frontend ConnectorRegistry
- [ ] Frontend has auth/connection page with both token and basic auth options
- [ ] Frontend has alerts viewing page with pagination

### Out of Scope

- Loki push API (writing logs to Loki) — Aurora is read-only for observability
- Loki ruler management (creating/editing alert rules) — managed in Grafana/Loki directly
- OAuth/SSO authentication to Loki — API token and basic auth cover all deployment types
- Real-time log streaming via WebSocket — batch query is sufficient for RCA

## Context

- Aurora already has 22+ connectors following a consistent pattern: Flask blueprint routes, Vault-backed credential storage, frontend ConnectorRegistry, Celery background tasks
- Closest existing connectors to model after: Datadog (log search + webhook alerts + RCA), Netdata (webhook + RCA + alerts page), Splunk (log search)
- Authentication: Loki supports Bearer token (Grafana Cloud, enterprise) and basic auth (self-hosted behind proxy). Both must be supported.
- Loki HTTP API endpoints: `/loki/api/v1/query_range`, `/loki/api/v1/query`, `/loki/api/v1/labels`, `/loki/api/v1/label/{name}/values`
- Alert webhooks come from Loki Ruler or Alertmanager configured to POST to Aurora's webhook endpoint
- Existing patterns: `@require_permission` decorator for RBAC, `store_tokens_in_db`/`get_token_data` for credentials, `db_pool.get_admin_connection()` for DB, `AlertCorrelator` for incident correlation

## Constraints

- **Tech stack**: Must follow existing Flask blueprint + Next.js patterns — no new frameworks
- **Auth storage**: Credentials must go through Vault via `store_tokens_in_db` — no plaintext DB storage
- **RBAC**: All routes must use `@require_permission` decorator
- **Database**: Must use existing `db_pool` connection pool and RLS patterns
- **Frontend**: Must use existing shadcn/ui components and `apiRequest` client pattern

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Support both API token + basic auth | Covers Grafana Cloud (token) and self-hosted (basic auth) deployments | — Pending |
| Follow Datadog/Netdata connector pattern | These are the most mature monitoring connectors with webhook + RCA support | — Pending |
| Store alerts in dedicated `loki_alerts` table | Consistent with `datadog_events`, `netdata_alerts` pattern | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-28 after initialization*
