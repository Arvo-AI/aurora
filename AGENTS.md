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
- **Docker Compose stack**: aurora-server (Flask API on :5080), celery_worker (background tasks), chatbot (WebSocket on :5006), frontend (Next.js on :3000), postgres (:5432), redis (:6379), vault (secrets :8200), seaweedfs (object storage :8333)
- **Backend** (server/): Flask REST API (main_compute.py), WebSocket chatbot (main_chatbot.py), Celery tasks, connectors for GCP/AWS/Azure/Datadog/New Relic/Grafana, LangGraph agent workflow
- **Frontend** (client/): Next.js 15, TypeScript, Tailwind CSS, shadcn/ui components, Auth.js authentication, path alias `@/*` → `./src/*`
- **Database**: PostgreSQL (aurora_db), Redis for Celery queue
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

## RCA Mode & Ask Mode

RCA (Root Cause Analysis) investigations are **explicitly read-only** per AGENTS.md guidelines. The agent can execute diagnostic queries in Ask mode during RCA without requiring Agent mode.

### How It Works

`is_read_only_command()` classifies the command's **operation** (its positional
arguments) and ignores option names and option values, then matches the leading
word of the **first** verb-looking positional against the verb sets:
- `describe-health-check` → leading word `describe` → read-only ✅
- `get-metric-statistics` → leading word `get` → read-only ✅
- `list-nodegroups` → leading word `list` → read-only ✅
- `terminate-instances --query Reservations` → leading word `terminate` → blocked ✅
  (the `--query` option can never supply the verb)
- `kubectl logs update-cache-cronjob-x` → verb is `logs`; the resource name that
  follows it is not scanned, so `update` doesn't flip the result ✅

This general approach works for **any** hyphenated diagnostic verb, not just hardcoded ones.

Six rules keep the gate fail-closed:
1. **Credential/token reads are denied** even though they mutate nothing. Rather
   than an ever-incomplete per-service list, any credential word (`key`, `keys`,
   `secret`, `credential`, `password`, `token`, `sas`, …) in the operation path
   blocks the command — this covers `sts get-session-token`, `eks get-token`,
   `ecr get-login-password`, `secretsmanager get-secret-value`, `kubectl get
   secrets`, `az storage account keys list`, `az signalr key list`, and the long
   tail of `<service> keys list` commands alike. Options that dump secrets
   (`--with-decryption`, `--expand-keys`) are blocked too. `aks get-credentials`
   and `container clusters get-credentials` are exempt: they only write a local
   kubeconfig and start every managed-Kubernetes investigation.
2. **Any write verb blocks the command**, including hyphenated mutations
   (`modify-*`, `terminate-*`, `reboot-*`, `delete-*`).
3. **Each shell segment is classified separately.** A write behind `&&`, `;`, a
   pipe, a newline, or a `$(...)`/backtick substitution blocks the whole command,
   so `kubectl get pods && kubectl delete pod x` is not read-only.
4. **A read cannot be used as a payload source.** The leading program must be a
   known cloud/k8s CLI (`aws`, `az`, `gcloud`, `kubectl`, `helm`, …) and every
   downstream segment must be a recognised text filter (`grep`, `jq`, `sort`,
   `head`, `awk`, `wc`, …). Both are allowlists, so an interpreter or exfil tool
   needs no enumeration — it simply isn't on either list. That blocks
   `kubectl get cm evil -o jsonpath='{.data.sh}' | bash` (read-only first
   segment, but the ConfigMap supplies the script) and `bash -c "aws ..."` /
   `sudo aws ...`, where the operation this function classifies isn't the one
   that runs. Redirections (`>`, `>>`, `<`) are blocked since they write a file
   regardless of what produced the bytes.
5. **Unknown operations default to blocked**, and an unparseable command
   (unbalanced quotes) is blocked rather than guessed at.
6. **Boolean switches don't swallow the verb** — `kubectl
   --insecure-skip-tls-verify get pods` stays read-only.

Tests: `server/tests/security/test_read_only_classifier.py`.

### Relationship to the Security settings guardrails

These are different questions and both are needed:

| | `is_read_only_command()` | Security tab (`org_command_policies`) |
|---|---|---|
| Asks | "is this a **write**?" | "is this **dangerous**?" |
| Scope | Ask mode only | every mode |
| On block | hard fail, "switch to Agent mode" | HITL prompt (Yes / No / Yes-Always) |
| Configurable | no | yes, per org, and can be disabled |

`kubectl delete pod x` is a routine operation no guardrail should block, but it
must fail in Ask mode. Conversely `rm -rf /` is caught by the denylist in *any*
mode. Ask mode can't delegate to the guardrails: they're HITL (there's no user to
prompt during a background RCA), org-configurable, and switchable off via
`GUARDRAILS_ENABLED=false` — Ask mode still has to hold when they're all off.

So keep this classifier narrow. It answers read-vs-write and defers everything
about *danger* to the shared layers (`signature_match.py`,
`_UNIVERSAL_DENY_RULES`, the LLM judge) via `gate_command()`. The one overlap is
deliberate: piping a read into an interpreter has to be refused here because the
wrapped command's verb is invisible to a read-vs-write check, and the shared
denylist only covers the `base64|sh` / `curl|sh` / `bash -c` spellings.

### Allowed RCA Diagnostic Commands (Ask Mode)

The following diagnostic queries are **always allowed** in Ask mode during RCA investigations:

**AWS Route 53 Health Checks:**
```bash
aws route53 describe-health-check --health-check-id <id>
aws route53 get-health-check-status --health-check-id <id>
```

**AWS CloudWatch Metrics:**
```bash
aws cloudwatch get-metric-statistics --namespace AWS/Route53 --metric-name HealthCheckStatus ...
aws cloudwatch describe-alarms --alarm-names <name>
aws cloudwatch list-metrics --namespace AWS/Route53
```

**AWS EKS Cluster Info:**
```bash
aws eks describe-cluster --name <cluster-name> --region <region>
aws eks describe-nodegroup --cluster-name <cluster> --nodegroup-name <nodegroup>
aws eks list-nodegroups --cluster-name <cluster>
```

**Kubernetes Diagnostics:**
```bash
kubectl get statefulsets -n <namespace>
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace>
kubectl top nodes
```

**Blocked in Ask mode (use Agent mode):** any mutation, and any credential- or
token-returning read such as `aws sts get-session-token`, `aws eks get-token`,
`aws ecr get-login-password`, or `aws ssm get-parameter --with-decryption`.

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
- **RLS-protected tables**: incidents, chat_sessions, user_tokens, user_connections, postmortems, llm_usage_tracking, incident_alerts, incident_lifecycle_events, connected_repos, execution_steps, and all monitoring event tables (datadog_events, grafana_alerts, etc.)
- **NOT RLS-protected**: users, incident_thoughts, incident_suggestions (CASCADE delete from incidents)
