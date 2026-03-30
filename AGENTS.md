# Aurora - Agent Guidelines

## Commands
- **Start dev**: `make dev` (builds & starts all containers)
- **Stop**: `make down`
- **View logs**: `make logs` (shows last 50 lines, follows)
- **Rebuild API**: `make rebuild-server` (rebuild aurora-server only)
- **Frontend lint**: `cd client && npm run lint`
- **Frontend build**: `cd client && npm run build`
- **Backend logs**: `docker logs -f aurora-celery_worker-1` (or `kubectl logs -f deployment/aurora-celery-worker`)
- **Deploy with Docker Compose**: `make prod` (production build)

## Docker deployments
- **Development**: Use `make dev-build` to build the project, `make dev` to start containers, and `make down` to stop them.
- **Production (prebuilt images)**: Use `make prod-prebuilt` to pull from GHCR, retag, and run. Use `make prod-local` to build from source instead. Use `make down` to stop.
- **Production (build from source)**: Use `make prod-local` or `make prod-build` for feature branch demos and custom builds.
- **Important**: Always update both `docker-compose.yaml` and `docker-compose.prod-local.yml` together to keep environment variables in sync.

## Architecture
- **Docker Compose stack**: aurora-server (Flask API on :5080), celery_worker (background tasks), chatbot (WebSocket on :5006), frontend (Next.js on :3000), postgres (:5432), weaviate (vector DB :8080), redis (:6379), vault (secrets :8200), seaweedfs (object storage :8333)
- **Backend** (server/): Flask REST API (main_compute.py), WebSocket chatbot (main_chatbot.py), Celery tasks, connectors for GCP/AWS/Azure/Datadog/New Relic/Grafana, LangGraph agent workflow
- **Frontend** (client/): Next.js 15, TypeScript, Tailwind CSS, shadcn/ui components, Auth.js authentication, path alias `@/*` → `./src/*`
- **Database**: PostgreSQL (aurora_db), Weaviate for semantic search, Redis for Celery queue
- **Secrets**: HashiCorp Vault (KV v2 engine at `aurora` mount)
- **Object Storage**: S3-compatible via SeaweedFS (default), supports AWS S3, Cloudflare R2, MinIO, etc.
- **Config**: Environment in `./.env`, GCP service account in `server/connectors/gcp_connector/*.json`

## Secrets Management (Vault)
Aurora uses HashiCorp Vault for secrets storage. User credentials (cloud provider tokens, API keys) are stored in Vault rather than directly in the database.

- **Persistent storage**: Vault uses file-based storage with data persisted in Docker volumes (`vault-data`, `vault-init`).
- **Auto-initialization**: The `vault-init` container automatically initializes and unseals Vault on startup, storing keys in the `vault-init` volume.
- **Secret references**: Stored in DB as `vault:kv/data/aurora/users/{secret_name}`, resolved at runtime.
- **Configuration**: `VAULT_ADDR`, `VAULT_TOKEN`, `VAULT_KV_MOUNT`, `VAULT_KV_BASE_PATH` env vars.
- **First run**: On first startup, check `vault-init` container logs for the root token. Set `VAULT_TOKEN` in `.env` to this value.
- **Test Vault**: `vault kv put aurora/users/test-secret value='hello'` then `vault kv get aurora/users/test-secret`

## Object Storage
Aurora uses S3-compatible object storage via `server/utils/storage/storage.py`. SeaweedFS is the default backend (Apache 2.0).

- **Storage module**: `from utils.storage.storage import get_storage_manager`
- **Design doc**: See `docs/oss/PLUGGABLE_STORAGE.md` for full details
- **SeaweedFS UI**: http://localhost:8888 (file browser), http://localhost:9333 (cluster status)
- **S3 API**: http://localhost:8333 (credentials: admin/admin)
- **Supports**: AWS S3, Cloudflare R2, Backblaze B2, GCS (via S3 interop), MinIO, any S3-compatible service

## Code Style
- **Python**: Use Flask blueprints in routes/, async with langchain/langgraph, psycopg2 for DB, logging at INFO level
- **TypeScript**: Strict mode, ESLint (next/core-web-vitals), no-unused-vars off in src/, use @/ imports, React 18 functional components
- **Naming**: Snake_case (Python), camelCase (TS/React), kebab-case (URLs)
- **Errors**: Flask error handlers, try/except with logging in Python
- **No tests found**: Check with team before adding test infrastructure

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Grafana Loki Connector**

A new connector for the Aurora platform that integrates Grafana Loki for centralized log aggregation. Users can connect their Loki instance, query logs via LogQL, receive alert webhooks that create incidents, and trigger automated root cause analysis — following the same patterns as existing Aurora connectors (Datadog, Netdata, Splunk).

**Core Value:** Users can connect their Loki instance and receive alert-driven incidents with automated RCA, closing the loop from log alert to investigation.

### Constraints

- **Tech stack**: Must follow existing Flask blueprint + Next.js patterns — no new frameworks
- **Auth storage**: Credentials must go through Vault via `store_tokens_in_db` — no plaintext DB storage
- **RBAC**: All routes must use `@require_permission` decorator
- **Database**: Must use existing `db_pool` connection pool and RLS patterns
- **Frontend**: Must use existing shadcn/ui components and `apiRequest` client pattern
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
### HTTP Client
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| `requests` | 2.32.x (already in project) | All Loki HTTP API calls | Aurora uses `requests` everywhere (Coroot, BigPanda, Datadog, Netdata). Zero new dependencies. `requests.Session` gives connection pooling and cookie persistence for free. No reason to introduce httpx or aiohttp for a synchronous Flask backend. |
- `httpx` or `aiohttp` -- Aurora's Flask backend is synchronous. Mixing async HTTP clients adds complexity with no benefit. The Weaviate client already brings httpx as a transitive dep, but all connector code uses `requests`.
- `loki-client` (PyPI) -- Inactive/abandoned (114 weekly downloads, no release in 12+ months per Snyk). Wraps the push API, not the query API Aurora needs.
- `python-loki-client` / `python-loki-clientv2` -- Beta/incomplete. The Loki HTTP API is simple enough that a thin `requests`-based client (following the BigPanda/Coroot pattern) is better than depending on an unmaintained wrapper.
- `python-logging-loki` / `loki-logger-handler` -- These are for *pushing* logs to Loki. Aurora is read-only (query + alerts), not a log shipper.
### Loki HTTP API (v1)
| Endpoint | Method | Purpose | Parameters | When to Use |
|----------|--------|---------|------------|-------------|
| `/loki/api/v1/query_range` | GET | Range log queries and metric queries over time | `query` (LogQL), `start`, `end`, `limit`, `step`, `direction` | Primary endpoint for log search and metric queries |
| `/loki/api/v1/query` | GET | Instant queries at a single point in time | `query` (LogQL), `limit`, `time`, `direction` | Spot checks, current state queries |
| `/loki/api/v1/labels` | GET | List all available label names | `start`, `end`, `since`, `query` | Building query UI, label discovery |
| `/loki/api/v1/label/{name}/values` | GET | List values for a specific label | `start`, `end`, `since`, `query` | Populating dropdowns, query building |
| `/loki/api/v1/series` | GET/POST | List unique stream label sets | `match[]` (repeatable), `start`, `end` | Stream discovery, topology mapping |
| `/loki/api/v1/index/stats` | GET | Query volume statistics (streams, chunks, entries, bytes) | `query`, `start`, `end` | Connection health check, instance metadata |
- `/loki/api/v1/push` -- Aurora is read-only, not a log shipper
- `/loki/api/v1/tail` -- WebSocket streaming; out of scope per PROJECT.md
- `/loki/api/v1/index/volume` and `/loki/api/v1/index/volume_range` -- Nice-to-have for dashboards but not required for MVP
- `/loki/api/v1/patterns` -- Pattern detection; not needed for core log query + alert flow
### Response Format
### Authentication
| Method | Header | Format | When Used |
|--------|--------|--------|-----------|
| Bearer Token | `Authorization` | `Bearer <token>` | Grafana Cloud, enterprise Loki behind token-based proxy |
| Basic Auth | `Authorization` | `Basic <base64(user:pass)>` | Self-hosted Loki behind nginx/proxy with basic auth, Grafana Cloud (instance_id:api_key) |
| Tenant ID | `X-Scope-OrgID` | `<tenant_id>` string | Multi-tenant Loki (`auth_enabled: true`). Required when Loki runs multi-tenant. |
- Loki itself has NO built-in authentication. Auth is always handled by a reverse proxy in front of Loki (nginx, HAProxy, OAuth2 proxy) or by Grafana Cloud's gateway.
- **Grafana Cloud format:** Basic auth where username = Loki instance ID (numeric), password = API key/access policy token. URL pattern: `https://<region>.grafana.net/loki/api/v1/...`
- **Self-hosted format:** Either basic auth via reverse proxy, or no auth at all (common in internal/VPN deployments). Bearer tokens if behind an auth gateway.
- **Multi-tenant header:** `X-Scope-OrgID` is required when `auth_enabled: true` (default). When `auth_enabled: false`, Loki uses tenant ID `fake` automatically. Pipe-separated for cross-tenant queries: `X-Scope-OrgID: tenantA|tenantB`.
- **Aurora implementation:** Support three auth modes: (1) Bearer token only, (2) Basic auth (username + password/token), (3) No auth (for internal Loki instances). Always allow optional `X-Scope-OrgID` as a separate field.
### Webhook Alert Payload (Alertmanager Format)
- The webhook endpoint in Aurora receives this POST. No authentication on the inbound webhook (same as Netdata pattern -- webhook URL contains user_id for routing).
- `status` is `"firing"` or `"resolved"`. Both must be handled.
- `alerts` is an array -- a single POST can contain multiple alerts.
- `startsAt` and `endsAt` are RFC3339 timestamps. `endsAt` of `"0001-01-01T00:00:00Z"` means the alert is still firing.
- The payload is NOT customizable from Alertmanager's standard webhook_configs. What you get is what you parse.
### LogQL Query Syntax Reference
- `rate()`, `count_over_time()`, `bytes_rate()`, `bytes_over_time()` -- log range aggregations
- `sum_over_time()`, `avg_over_time()`, `max_over_time()`, `min_over_time()` -- unwrapped value aggregations
- `sum`, `avg`, `min`, `max`, `count`, `topk`, `bottomk` -- vector aggregation operators
- `absent_over_time()` -- returns 1 when no data exists (useful for "silence" alerting)
### Client Architecture Pattern
- Follows BigPanda pattern: class with `_request` helper, per-endpoint methods, custom exception class
- Uses `requests.Session` like Coroot: enables connection pooling across calls
- No client caching needed (unlike Coroot's session-cookie auth) since token/basic auth is stateless
- No retry logic in client (keep it simple like BigPanda); Flask routes handle retries if needed
### Infrastructure
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| PostgreSQL | Existing | `loki_alerts` table for webhook alert storage | Follows `datadog_events`, `netdata_alerts` pattern. Same `db_pool` connection. |
| HashiCorp Vault | Existing | Credential storage for Loki auth tokens | Required by Aurora -- `store_tokens_in_db` / `get_token_data` with `"loki"` service key. |
| Celery + Redis | Existing | Background RCA processing on alert webhook | Follows Netdata pattern: webhook -> store alert -> `process_loki_alert.delay()` -> background chat RCA. |
| Flask Blueprints | Existing | Route organization | `loki_bp = Blueprint("loki", __name__)` registered at `/loki/` prefix. |
## Alternatives Considered
| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| HTTP Client | `requests` (existing) | `httpx` | Would add a dependency to the connector layer. All other connectors use `requests`. Async not needed in Flask. |
| Loki Client Lib | Custom thin client | `loki-client` (PyPI) | Abandoned (no updates 12+ months), only 114 weekly downloads, wraps push API not query API. |
| Loki Client Lib | Custom thin client | `python-loki-clientv2` (PyPI) | Beta/incomplete, adds external dependency risk for a simple REST API. |
| Auth Storage | Vault via `store_tokens_in_db` | Direct DB storage | Violates Aurora's security model. All credentials MUST go through Vault. |
| Background Tasks | Celery | Inline processing | Webhook processing must be non-blocking. Celery is how Datadog/Netdata handle it. |
## New Dependencies
- `requests` 2.32.5 -- HTTP client
- `flask` -- Blueprint routes
- `celery` -- Background tasks
- `psycopg2` -- Database operations via `db_pool`
## File Structure (Following Existing Patterns)
## Sources
- [Loki HTTP API Reference](https://grafana.com/docs/loki/latest/reference/loki-http-api/) -- Official endpoint documentation (HIGH confidence)
- [Loki Authentication](https://grafana.com/docs/loki/latest/operations/authentication/) -- Auth methods (HIGH confidence)
- [Loki Multi-Tenancy](https://grafana.com/docs/loki/latest/operations/multi-tenancy/) -- X-Scope-OrgID header (HIGH confidence)
- [Loki v3.5 Release Notes](https://grafana.com/docs/loki/latest/release-notes/v3-5/) -- Current API stability (HIGH confidence)
- [Grafana Loki Releases](https://github.com/grafana/loki/releases) -- v3.7.1 current (HIGH confidence)
- [Loki Alerting and Recording Rules](https://grafana.com/docs/loki/latest/alert/) -- Ruler/Alertmanager integration (HIGH confidence)
- [LogQL Metric Queries](https://grafana.com/docs/loki/latest/query/metric_queries/) -- Query function reference (HIGH confidence)
- [Grafana Cloud Loki HTTP API Forum](https://community.grafana.com/t/http-api-to-grafana-cloud-loki/90607) -- Cloud auth format (MEDIUM confidence, community source)
- [Alertmanager Webhook Payload](https://gist.github.com/mobeigi/5a96f326bc06c7d6f283ecb7cb083f2b) -- Webhook JSON structure (MEDIUM confidence, verified against Prometheus Alertmanager source)
- [Snyk: loki-client Health](https://snyk.io/advisor/python/loki-client) -- Package health assessment showing inactive/abandoned status (MEDIUM confidence)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd:quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd:debug` for investigation and bug fixing
- `/gsd:execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->

<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
