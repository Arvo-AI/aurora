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

## New Connector Checklist

Every new connector (or connector route file) **must** satisfy all of the following before merge. CI enforces RBAC via `server/tests/architectural/test_connector_rbac.py`.

### RBAC (mandatory — CI-enforced)
- [ ] Every route function decorated with `@require_permission("connectors", "read")` (GET/status) or `@require_permission("connectors", "write")` (POST/connect/disconnect)
- [ ] Import from `utils.auth.rbac_decorators import require_permission`
- [ ] Route function accepts `user_id` as first positional arg (injected by decorator)
- [ ] No manual `get_user_id_from_request()` or OPTIONS handling (decorator does both)
- [ ] Webhook/callback routes exempt only if authenticated via HMAC/signing secret or OAuth state param

### Skills Integration
- [ ] `SKILL.md` created at `server/chat/backend/agent/skills/integrations/<name>/SKILL.md`
- [ ] Skill registered in `server/chat/backend/agent/skills/registry.py` with `check_connection` callable
- [ ] `rca_priority` set appropriately (lower = loaded earlier in RCA prompt)
- [ ] RCA workflow section is **read-only** — agent searches but never writes during RCA

### Agent Tools
- [ ] Tools registered as LangChain `StructuredTool` in `server/chat/backend/agent/tools/cloud_tools.py`
- [ ] Tools gated behind `is_<name>_connected(user_id)` check
- [ ] `run_<name>_tool()` pattern with `_do(client)` callback for auth + error handling

### Frontend
- [ ] Provider added to `ConnectorRegistry.ts` with proper `stateEvent` name
- [ ] Status query uses `revalidateOnEvents` with the provider's state event
- [ ] Disconnect triggers `window.dispatchEvent(new Event('<name>StateChanged'))`
- [ ] Event-triggered revalidation uses `queryClient.invalidate()` (not `.fetch()`)

### Token Storage
- [ ] Tokens stored via `store_tokens_in_db(user_id, payload, "<name>")`
- [ ] Token retrieval via `get_token_data(user_id, "<name>")`
- [ ] Disconnect deletes via `delete_user_secret(user_id, "<name>")`

### Blueprint Registration
- [ ] Blueprint registered in `server/main_compute.py` with appropriate `url_prefix`
- [ ] Connector directory added to `CONNECTOR_DIRS` in `server/tests/architectural/test_connector_rbac.py`

## Security Invariants

These rules are enforced by automated review (CodeRabbit) and **must** be followed during development to avoid review churn.

### Credential Isolation
- Never mutate shared process state (e.g. `os.environ`, on-disk CLI config) with per-user credentials. Credentials must be scoped per-invocation and passed explicitly.
- Subprocess calls (`subprocess.run`, `Popen`, etc.) must receive an explicit `env` parameter with only the variables they need — never inherit the full server environment.
- Never log secrets, tokens, credentials, or full OAuth/auth responses at any log level.

### Client ↔ Backend Boundary
- All client-to-backend HTTP requests must go through the Next.js API route proxy using `forwardRequest` from `@/lib/backend-proxy`. Never call the Flask backend directly from browser code.
- Never derive, store, or trust user identity (`user_id`, `org_id`) on the client side. Identity resolution is server-side only.
- All inter-service calls (frontend server → backend, MCP → backend) must include the internal API secret header.

### HTML & XSS
- Never render untrusted HTML without sanitization. Any use of `dangerouslySetInnerHTML` must sanitize the content first.

### Infrastructure
- Never mount the Docker socket (`/var/run/docker.sock`) into application containers.
- CORS handling must go through the centralized utility — never add ad-hoc `Access-Control-*` headers in route files.
- Environment variable changes must stay in sync across all Docker Compose files (`docker-compose.yaml`, `docker-compose.prod-local.yml`) and `.env.example`.

### Route & Connector Security
- Routes must use RBAC decorators (`@require_permission`), never manual auth checks.
- Token storage must go through centralized helpers (`store_tokens_in_db` / `get_token_data`), not ad-hoc DB writes.
- OAuth callbacks must never log full token responses.

## Security Invariants

These rules are enforced by automated review (CodeRabbit) and **must** be followed during development to avoid review churn.

### Credential Isolation
- Never mutate shared process state (e.g. `os.environ`, on-disk CLI config) with per-user credentials. Credentials must be scoped per-invocation and passed explicitly.
- Subprocess calls (`subprocess.run`, `Popen`, etc.) must receive an explicit `env` parameter with only the variables they need — never inherit the full server environment.
- Never log secrets, tokens, credentials, or full OAuth/auth responses at any log level.

### Client ↔ Backend Boundary
- All client-to-backend HTTP requests must go through the Next.js API route proxy using `forwardRequest` from `@/lib/backend-proxy`. Never call the Flask backend directly from browser code.
- Never derive, store, or trust user identity (`user_id`, `org_id`) on the client side. Identity resolution is server-side only.
- All inter-service calls (frontend server → backend, MCP → backend) must include the internal API secret header.

### HTML & XSS
- Never render untrusted HTML without sanitization. Any use of `dangerouslySetInnerHTML` must sanitize the content first.

### Infrastructure
- Never mount the Docker socket (`/var/run/docker.sock`) into application containers.
- CORS handling must go through the centralized utility — never add ad-hoc `Access-Control-*` headers in route files.
- Environment variable changes must stay in sync across all Docker Compose files (`docker-compose.yaml`, `docker-compose.prod-local.yml`) and `.env.example`.

### Route & Connector Security
- Routes must use RBAC decorators (`@require_permission`), never manual auth checks.
- Token storage must go through centralized helpers (`store_tokens_in_db` / `get_token_data`), not ad-hoc DB writes.
- OAuth callbacks must never log full token responses.

## Code Style
- **Python**: Use Flask blueprints in routes/, async with langchain/langgraph, psycopg2 for DB, logging at INFO level
- **TypeScript**: Strict mode, ESLint (next/core-web-vitals), no-unused-vars off in src/, use @/ imports, React 18 functional components
- **Naming**: Snake_case (Python), camelCase (TS/React), kebab-case (URLs)
- **Errors**: Flask error handlers, try/except with logging in Python
- **No tests found**: Check with team before adding test infrastructure

## Row-Level Security (RLS) — Critical for Celery Tasks
PostgreSQL tables use `FORCE ROW LEVEL SECURITY`. All queries on RLS-protected tables require `myapp.current_org_id` set on the connection — without it, queries silently return 0 rows.

- **Flask requests**: RLS vars are set automatically by `_set_rls_vars()` in the connection pool
- **Celery workers / background tasks**: There is NO Flask request context, so RLS vars are NEVER set automatically. You MUST call `set_rls_context(cursor, conn, user_id)` (from `utils.auth.stateless_auth`) before any query on an RLS-protected table.
- **Helper**: `from utils.auth.stateless_auth import set_rls_context; org_id = set_rls_context(cursor, conn, user_id, log_prefix="[YourTask]")`
- **Cross-org tasks** (iterating all users): Query the `users` table first (NOT RLS-protected), then iterate per-org setting RLS context before querying RLS tables.
- **RLS-protected tables**: incidents, chat_sessions, user_tokens, user_connections, postmortems, llm_usage_tracking, incident_alerts, incident_lifecycle_events, github_connected_repos, execution_steps, and all monitoring event tables (datadog_events, grafana_alerts, etc.)
- **NOT RLS-protected**: users, incident_thoughts, incident_suggestions (CASCADE delete from incidents)
