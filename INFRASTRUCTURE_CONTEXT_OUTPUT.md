# Aurora Infrastructure Context

## Overview

Aurora is an open-source AI-powered incident management and root cause analysis platform built by Arvo (Canadian AI company). It autonomously investigates cloud infrastructure incidents using LangGraph-orchestrated AI agents, integrating with 30+ tools and services.

**Repository:** [Arvo-AI/aurora](https://github.com/Arvo-AI/aurora) (main branch)  
**License:** Apache 2.0  
**Architecture:** Monorepo with server (Python/Flask), client (Next.js), kubectl-agent (Go), and Helm charts

---

## Environments

### Production
- **Platform:** Google Kubernetes Engine (GKE) Autopilot
- **Deployment:** Helm chart (`deploy/helm/aurora/`)
- **Scaling:** HPA on server and celery-worker; PDBs (minAvailable: 1) on server, chatbot, celery-worker
- **Auth:** GKE Workload Identity with per-service ServiceAccount annotations
- **Monitoring:** GCP Monitoring — alerts for Pod Restart Rate (>5, excludes kubectl-agent), log-based metric `aurora_container_crash` (tracebacks, panics, non-zero exits)
- **Control Plane Logging:** API_SERVER, SCHEDULER, CONTROLLER_MANAGER enabled

### Development / Self-Hosted
- **Platform:** Docker Compose (docker-compose.yaml for dev, docker-compose.prod-local.yml for production-like local, docker-compose.airtight.yml for security-hardened variant)
- **Deployment:** `make up` / `docker-compose up`
- **Alternative:** VM deployment via `deploy/vm-deploy.sh`

### Airtight (Security-Hardened)
- **Platform:** Docker Compose or Kubernetes
- **Features:** No network egress from terminal pods, SigmaHQ rule enforcement, enhanced guardrails
- **Images:** Built via `publish-airtight.yml` workflow

---

## Services

### Core Application Services

| Service | Language | Port | Image | Description |
|---------|----------|------|-------|-------------|
| **server** | Python/Flask | 5000 | `ghcr.io/arvo-ai/aurora-server` | REST API backend, Gunicorn (2 workers × 8 threads) |
| **chatbot** | Python/LangGraph | 5001 | `ghcr.io/arvo-ai/aurora-chatbot` | AI agent for RCA, WebSocket-based |
| **celery-worker** | Python/Celery | — | `ghcr.io/arvo-ai/aurora-server` | Async task processing (RCA, discovery, postmortems) |
| **celery-beat** | Python/Celery | — | `ghcr.io/arvo-ai/aurora-server` | Periodic task scheduler |
| **mcp** | Python | 8811 | `ghcr.io/arvo-ai/aurora-mcp` | Model Context Protocol server for tool integration |
| **frontend** | TypeScript/Next.js | 3000 | `ghcr.io/arvo-ai/aurora-frontend` | Web UI (Bun build, Nginx serve) |
| **searxng** | Python | 8080 | `searxng/searxng` | Web search engine for agent queries |
| **t2v-transformers** | Python | 8080 | `semitechnologies/transformers-inference` | Text-to-vector embeddings for knowledge base |
| **user-terminal** | Multi | — | `ghcr.io/arvo-ai/aurora-user-terminal` | Sandboxed pod for executing cloud CLI commands |

### Data Stores (StatefulSets in K8s)

| Service | Port | Storage | Description |
|---------|------|---------|-------------|
| **PostgreSQL** | 5432 | PVC | Primary relational DB with RLS, ThreadedConnectionPool (max 20, 5s wait timeout) |
| **Redis** | 6379 | PVC | Celery broker/backend, caching (connector status 30s TTL) |
| **Weaviate** | 8080 | PVC | Vector database for knowledge base RAG |
| **Memgraph** | 7687, 7444 | PVC | Graph database for infrastructure topology |
| **MinIO** | 9000, 9001 | PVC | S3-compatible object storage (files, KB docs) |
| **Vault** | 8200 | PVC | HashiCorp Vault for secrets management |

### External Component

| Service | Description |
|---------|-------------|
| **kubectl-agent** | Deployed in customer clusters, WebSocket back to Aurora, org-scoped auth tokens |

---

## Service Dependencies

```
Frontend (3000) → Server API (5000) → PostgreSQL (5432)
                                     → Redis (6379)
                                     → Vault (8200)
                                     → Memgraph (7687)

Frontend (3000) → Chatbot WS (5001) → Redis (6379) [via Celery]
                                     → PostgreSQL (5432)
                                     → Weaviate (8080) [KB search]
                                     → SearXNG (8080) [web search]
                                     → MCP Server (8811) [tool calls]

Celery Worker → Redis (6379) [broker]
             → PostgreSQL (5432)
             → Weaviate (8080)
             → External APIs (via connectors)

MCP Server (8811) → Server API (5000) [connector status]
                  → Vault (8200) [credentials]
                  → External APIs

t2v-transformers → Weaviate (8080) [vectorization module]

kubectl-agent (customer cluster) → Server WS (5000) [outbound WebSocket]
```

---

## CI/CD Pipelines

### GitHub Actions Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `build.yml` | PR | Lint, test, validate |
| `publish-images.yml` | Release tag / manual | Build multi-arch Docker images → GHCR |
| `publish-helm.yml` | Release tag / manual | Package Helm chart → GitHub Pages OCI repo |
| `publish-airtight.yml` | Release tag / manual | Build security-hardened images → GHCR |
| `claude-code-review.yml` | PR | AI code review via Claude |
| `claude.yml` | Issue/PR comment | Claude Code agent for development tasks |
| `linters.yml` | PR | Code quality (ESLint, Ruff, etc.) |
| `validate-env-vars.yml` | PR | Environment variable consistency check |
| `docs.yml` | Push to main | Documentation generation |

### Container Registry
- **Registry:** GitHub Container Registry (ghcr.io/arvo-ai/aurora-*)
- **Images:** aurora-server, aurora-chatbot, aurora-frontend, aurora-mcp, aurora-user-terminal
- **Architectures:** linux/amd64, linux/arm64

### Helm Repository
- **URL:** GitHub Pages (gh-pages branch)
- **Charts:** aurora (main platform), aurora-kubectl-agent (customer cluster agent)

---

## Integrations (Connectors)

### Cloud Providers
- **AWS** — IAM role assumption, multi-account support
- **Azure** — MSAL/OAuth with lazy initialization
- **GCP** — OAuth + Service Account, Workload Identity, deterministic SA email derivation
- **OVH** — API key auth
- **Scaleway** — API key auth

### Source Control
- **GitHub** — App-based auth (installation tokens, HMAC webhooks), optional OAuth fallback (`GITHUB_AUTH_MODE`: app|oauth|hybrid)
- **Bitbucket** — OAuth
- **GitLab** — OAuth/token

### CI/CD
- **Jenkins** — API token, Core/Pipeline/Blue Ocean APIs
- **CloudBees** — Same as Jenkins
- **Spinnaker** — API token

### Monitoring & Observability
- **Datadog** — API/App keys
- **New Relic** — API key, NRQL queries
- **Grafana** — API token
- **Coroot** — eBPF service maps
- **Dynatrace** — API token
- **ThousandEyes** — API token
- **Netdata** — API token
- **Sentry** — DSN/token
- **Splunk** — HEC/API token

### Incident Management
- **PagerDuty** — API key, webhook events
- **OpsGenie/JSM** — API key
- **BigPanda** — API token
- **incident.io** — API token

### Communication
- **Slack** — Bot token, slash commands
- **Google Chat** — Service account

### Documentation
- **Confluence** — Atlassian OAuth
- **Notion** — OAuth
- **SharePoint** — MSAL/OAuth

### Networking
- **Cloudflare** — API token
- **Tailscale** — API key, tailnet access

---

## Security Architecture

### Authentication & Authorization
- **User Auth:** Supabase Auth (JWT-based)
- **Multi-tenancy:** PostgreSQL Row-Level Security (RLS)
- **RBAC:** Casbin model (rbac_model.conf) — roles: admin, member, viewer
- **API Auth:** Bearer tokens, org-scoped access

### Agent Security Guardrails
- **Input Rail:** SigmaHQ signature detection (server/guardrails/input_rail.py)
- **Prompt Injection:** NVIDIA NeMo detection
- **Command Policies:** Allowlist/denylist regex patterns (server/routes/command_policies.py)
- **Terminal Isolation:** Sandboxed Kubernetes pods for CLI execution (user-terminal image)
- **Airtight Mode:** No network egress from terminal pods

### Secrets Management
- **Vault:** HashiCorp Vault stores all integration credentials
- **Kubernetes:** Secrets for app, backend, db, llm configurations
- **Environment:** `.env` file for Docker Compose deployments

---

## Network Topology (Kubernetes)

### Ingress
- **Controller:** Nginx Ingress
- **TLS:** cert-manager with Let's Encrypt
- **Routes:**
  - `/` → frontend service (3000)
  - `/api/*` → server service (5000)
  - `/ws/*` → chatbot service (5001)
  - `/mcp/*` → mcp service (8811)

### Network Policies
- Pod isolation via `deploy/helm/aurora/templates/pod-isolation.yaml`
- Terminal pods restricted from accessing internal services (airtight mode)

### Service Mesh
- No service mesh; direct pod-to-pod communication via Kubernetes Services

---

## Key Configuration

### Environment Variables (from .env.example)
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection string
- `WEAVIATE_URL` — Weaviate endpoint
- `VAULT_ADDR` / `VAULT_TOKEN` — Vault access
- `MEMGRAPH_HOST` / `MEMGRAPH_PORT` — Graph DB
- `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` — Object storage
- `LLM_PROVIDER` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — AI model config
- `SUPABASE_URL` / `SUPABASE_KEY` — Auth provider
- `GITHUB_APP_ID` / `GITHUB_PRIVATE_KEY` / `GITHUB_WEBHOOK_SECRET` — GitHub App
- `SEARXNG_URL` — Web search endpoint
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — Task queue

### Helm Values (deploy/helm/aurora/values.yaml)
- Per-service replica counts, resource limits, image tags
- Ingress host configuration
- Storage class and PVC sizes
- ServiceAccount annotations for Workload Identity
- HPA min/max replicas and CPU thresholds

---

## Recent Development Activity (May 2026)

### Active Focus Areas
1. **Multi-agent RCA reliability** — fixing hallucination, routing, time correlation
2. **GitHub App migration** — replacing OAuth with App-based auth
3. **GKE Autopilot stability** — PDBs, HPA, eviction handling
4. **Database resilience** — connection pool wait-with-timeout, advisory locks
5. **MCP connector accuracy** — fixing callable_now status via backend API caching
6. **GCP performance** — parallelized IAM checks, deterministic SA derivation

### Key Contributors
- **beng360** — Infrastructure, backend, Helm, CI/CD
- **Harrio-6** — Backend fixes, org-scoping, postmortem gating
- **isiddharthsingh** — RCA agent, GitHub integration
- **damianloch** — Features
- **Zarlanx** — Features

### Development Tools
- CodeRabbit (automated PR review)
- Claude Code (AI-assisted development)
- Linear (issue tracking, DEV-XXXX numbering)
- CODEOWNERS for review routing

---

## Deployment Procedures

### Kubernetes (Production)
```bash
# Add Helm repo
helm repo add aurora https://arvo-ai.github.io/aurora

# Install/upgrade
helm upgrade --install aurora aurora/aurora \
  --namespace aurora --create-namespace \
  -f values-production.yaml

# kubectl-agent (customer clusters)
helm upgrade --install aurora-kubectl-agent aurora/aurora-kubectl-agent \
  --set serverUrl=wss://your-aurora-instance/ws/kubectl \
  --set authToken=<org-token> \
  --set clusterId=<cluster-id>
```

### Docker Compose (Development)
```bash
cp .env.example .env  # Configure environment
make up               # Start all services
# OR
docker-compose up -d
```

### VM Deployment
```bash
./deploy/preflight.sh  # Check prerequisites
./deploy/vm-deploy.sh  # Deploy to VM
```

---

## Monitoring & Alerting

### GCP Monitoring (Production)
- **Pod Restart Rate** — threshold >5 in 10min, excludes kubectl-agent
- **Container Crash** — log-based metric, fires on tracebacks/panics/non-zero exits
- **Control Plane** — API server, scheduler, controller manager logging enabled

### Application Health
- `/health` endpoint on server (port 5000)
- WebSocket heartbeat on chatbot (port 5001)
- Celery worker health via Redis broker connectivity

### Observability Stack (Self-Monitoring)
- Structured logging (JSON) from all Python services
- PostgreSQL connection pool metrics (pool exhaustion warnings)
- Celery task success/failure rates via Redis
