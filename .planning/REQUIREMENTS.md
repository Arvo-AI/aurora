# Requirements: Grafana Loki Connector

**Defined:** 2026-03-28
**Core Value:** Users can connect their Loki instance and receive alert-driven incidents with automated RCA

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Connection Management

- [x] **CONN-01**: User can connect a Loki instance by providing URL and Bearer token authentication
- [x] **CONN-02**: User can connect a Loki instance by providing URL and Basic auth (username/password) credentials
- [x] **CONN-03**: User can connect a Loki instance with no authentication (internal/VPN deployments)
- [x] **CONN-04**: User can optionally specify a tenant ID (X-Scope-OrgID) for multi-tenant Loki deployments
- [x] **CONN-05**: Connection is validated via two-step check (/ready endpoint then /loki/api/v1/labels)
- [x] **CONN-06**: User can check Loki connection status and see instance metadata (URL, auth type, tenant)
- [x] **CONN-07**: User can disconnect Loki and have all stored credentials removed from Vault

### Log Querying

- [ ] **QUERY-01**: User can execute LogQL range queries via /loki/api/v1/query_range with time range and limit
- [ ] **QUERY-02**: User can execute LogQL instant queries via /loki/api/v1/query
- [ ] **QUERY-03**: User can list all stream labels via /loki/api/v1/labels
- [ ] **QUERY-04**: User can list values for a specific label via /loki/api/v1/label/{name}/values
- [ ] **QUERY-05**: User can execute LogQL metric queries (rate, count_over_time) via query_range endpoint
- [ ] **QUERY-06**: User can discover log series via /loki/api/v1/series with label matchers

### Alert Pipeline

- [ ] **ALERT-01**: System receives Loki Ruler/Alertmanager webhook alerts at a dedicated endpoint
- [x] **ALERT-02**: System auto-detects and normalizes both Alertmanager v4 and Grafana Unified webhook formats
- [x] **ALERT-03**: Webhook alerts are stored in a dedicated loki_alerts database table with full payload
- [ ] **ALERT-04**: Webhook alerts create incidents via AlertCorrelator with Loki-specific metadata
- [ ] **ALERT-05**: Webhook alerts trigger automated RCA via Celery background chat
- [x] **ALERT-06**: Loki-specific RCA prompt builder provides structured alert context for investigation

### Frontend

- [ ] **FE-01**: Loki connector is registered in the frontend ConnectorRegistry under "Monitoring" category
- [ ] **FE-02**: Auth/connection page supports Bearer token, Basic auth, and no-auth modes with toggle
- [ ] **FE-03**: Auth page displays webhook URL with Loki/Alertmanager setup instructions after connection
- [ ] **FE-04**: Next.js API routes proxy backend Loki endpoints (connect, status, webhook-url)

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Frontend

- **FE-05**: Alerts viewing page with paginated list of received Loki alerts and status badges
- **FE-06**: Log query interface for interactive LogQL exploration

### Enrichment

- **ENRICH-01**: Auto-query surrounding logs when an alert fires for RCA context injection
- **ENRICH-02**: Detected fields discovery via /loki/api/v1/detected_fields (Loki 3.0+)
- **ENRICH-03**: Log volume estimation for query planning

## Out of Scope

| Feature | Reason |
|---------|--------|
| Loki push API (writing logs) | Aurora is read-only for observability |
| Loki ruler management (creating/editing alert rules) | Managed in Grafana/Loki directly |
| OAuth/SSO authentication to Loki | API token and basic auth cover 95%+ of deployments |
| Real-time log streaming via WebSocket | Batch query sufficient for RCA |
| Custom LogQL query editor UI | Complex UI component, defer to v2+ |
| Prometheus-compatible query API surface | Loki API v1 is the standard |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| CONN-01 | Phase 1 | Complete |
| CONN-02 | Phase 1 | Complete |
| CONN-03 | Phase 1 | Complete |
| CONN-04 | Phase 1 | Complete |
| CONN-05 | Phase 1 | Complete |
| CONN-06 | Phase 1 | Complete |
| CONN-07 | Phase 1 | Complete |
| QUERY-01 | Phase 4 | Pending |
| QUERY-02 | Phase 4 | Pending |
| QUERY-03 | Phase 4 | Pending |
| QUERY-04 | Phase 4 | Pending |
| QUERY-05 | Phase 4 | Pending |
| QUERY-06 | Phase 4 | Pending |
| ALERT-01 | Phase 2 | Pending |
| ALERT-02 | Phase 2 | Complete |
| ALERT-03 | Phase 2 | Complete |
| ALERT-04 | Phase 2 | Pending |
| ALERT-05 | Phase 2 | Pending |
| ALERT-06 | Phase 2 | Complete |
| FE-01 | Phase 3 | Pending |
| FE-02 | Phase 3 | Pending |
| FE-03 | Phase 3 | Pending |
| FE-04 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 23 total
- Mapped to phases: 23
- Unmapped: 0

---
*Requirements defined: 2026-03-28*
*Last updated: 2026-03-28 after roadmap creation*
