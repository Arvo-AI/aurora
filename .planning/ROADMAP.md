# Roadmap: Grafana Loki Connector

## Overview

This roadmap delivers a Grafana Loki connector for Aurora in four phases, following the dependency graph from foundational client/credentials through the alert-to-RCA pipeline, frontend wiring, and query capabilities. Phase 1 establishes the LokiClient and connection management (the foundation everything depends on). Phase 2 builds the alert pipeline that delivers Aurora's core value -- alerts trigger incidents trigger RCA. Phase 3 wires the frontend so users can connect and see webhook instructions. Phase 4 adds LogQL query capabilities for investigation and agent tooling.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3, 4): Planned milestone work
- Decimal phases (e.g., 2.1): Urgent insertions (marked with INSERTED)

- [ ] **Phase 1: Connection Management** - LokiClient, three auth modes, Vault credential storage, connect/status/disconnect routes
- [ ] **Phase 2: Alert Pipeline** - Webhook endpoint, dual-format detection, alert storage, incident creation, automated RCA
- [ ] **Phase 3: Frontend Integration** - ConnectorRegistry entry, auth/connection page, webhook URL display, API route proxies
- [ ] **Phase 4: Query Capabilities** - LogQL range/instant/metric queries, label and series discovery endpoints

## Phase Details

### Phase 1: Connection Management
**Goal**: Users can connect their Loki instance to Aurora with any supported auth mode and verify the connection works
**Depends on**: Nothing (first phase)
**Requirements**: CONN-01, CONN-02, CONN-03, CONN-04, CONN-05, CONN-06, CONN-07
**Success Criteria** (what must be TRUE):
  1. User can connect a Loki instance using Bearer token, Basic auth, or no-auth mode and credentials are stored in Vault
  2. User can specify a tenant ID for multi-tenant Loki deployments and the X-Scope-OrgID header is sent on all requests
  3. User can check connection status and see instance metadata (URL, auth type, tenant ID)
  4. Connection validation performs a two-step check (reachability then credential verification) and rejects invalid configurations
  5. User can disconnect Loki and all credentials are removed from Vault
**Plans**: 2 plans
Plans:
- [x] 01-01-PLAN.md -- LokiClient HTTP client class and loki_alerts database table
- [ ] 01-02-PLAN.md -- Flask routes (connect/status/disconnect) and blueprint registration

### Phase 2: Alert Pipeline
**Goal**: Loki alerts arrive via webhook, are stored and deduplicated, create incidents, and trigger automated root cause analysis
**Depends on**: Phase 1
**Requirements**: ALERT-01, ALERT-02, ALERT-03, ALERT-04, ALERT-05, ALERT-06
**Success Criteria** (what must be TRUE):
  1. System receives webhook alerts from both Alertmanager v4 and Grafana Unified Alerting formats at a dedicated endpoint
  2. Alerts are auto-detected by format, normalized, deduplicated by hash, and stored in the loki_alerts table with full payload
  3. Each new alert creates an incident via AlertCorrelator with Loki-specific metadata (labels, annotations, source)
  4. Each new alert triggers automated RCA via Celery background chat with a Loki-specific prompt containing structured alert context
**Plans**: TBD

### Phase 3: Frontend Integration
**Goal**: Users can connect Loki, configure webhook delivery, and manage connection status through the Aurora UI
**Depends on**: Phase 1, Phase 2
**Requirements**: FE-01, FE-02, FE-03, FE-04
**Success Criteria** (what must be TRUE):
  1. Loki appears in the ConnectorRegistry under the Monitoring category and navigates to the auth page
  2. User can connect Loki from the auth page by choosing Bearer token, Basic auth, or no-auth mode and providing credentials
  3. After connecting, user sees the webhook URL with copy-to-clipboard and Alertmanager/Loki Ruler setup instructions
  4. Frontend API routes correctly proxy connect, status, and webhook-url requests to the backend
**Plans**: TBD
**UI hint**: yes

### Phase 4: Query Capabilities
**Goal**: Users and the LangGraph agent can query logs, discover labels, and execute metric queries against the connected Loki instance
**Depends on**: Phase 1
**Requirements**: QUERY-01, QUERY-02, QUERY-03, QUERY-04, QUERY-05, QUERY-06
**Success Criteria** (what must be TRUE):
  1. User can execute LogQL range queries with time range and limit and receive parsed log stream results
  2. User can execute LogQL instant queries for metric-type expressions and receive vector/matrix results
  3. User can list all stream labels and values for a specific label to assist with query building
  4. User can execute LogQL metric queries (rate, count_over_time) and receive numeric results
  5. User can discover log series via label matchers to explore available log streams
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Connection Management | 0/2 | Planning complete | - |
| 2. Alert Pipeline | 0/0 | Not started | - |
| 3. Frontend Integration | 0/0 | Not started | - |
| 4. Query Capabilities | 0/0 | Not started | - |
