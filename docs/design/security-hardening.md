# Security Hardening — Design Doc

## Customer

**PNC** — Steven Freeman, Sr. Site Reliability Engineer, SRE Environment Observability Team

## Problem

Steven asked whether Aurora has hardened its communication paths. At the time of inquiry:

- **No TLS termination in the repo.** The Docker Compose production stack (`docker-compose.prod-local.yml`) exposed all services — including the frontend (`:3000`), API (`:5080`), and WebSocket (`:5006`) — on bare HTTP with no reverse proxy or TLS in front. K8s deployments use Nginx Ingress with a TLS cert, but Docker Compose had no equivalent.
- **Internal services publicly reachable.** PostgreSQL (`:5432`), Redis (`:6379`), Vault (`:8200`), Weaviate (`:8080`), Memgraph (`:7687`), and SeaweedFS ports were all bound to `0.0.0.0`, meaning any host on the network could reach them.
- **CORS origin reflection.** While `main_compute.py` configures strict CORS via `CORS(app, origins=FRONTEND_URL)`, the helper `create_cors_response()` in `server/utils/web/cors_utils.py` reflects whichever `Origin` header the client sends, bypassing the whitelist for preflight responses.

## Solution & Exact Δ

### Transport Layer — Traefik Reverse Proxy (Docker Compose)

| Before | After |
|--------|-------|
| Frontend, API, WebSocket exposed directly on HTTP | Traefik terminates TLS on ports 80/443; services have no host-bound ports |
| No security headers | HSTS, X-Frame-Options DENY, X-Content-Type-Options nosniff, XSS filter, strict Referrer-Policy |
| No auto-cert provisioning | Let's Encrypt ACME via Traefik's built-in cert resolver |
| All internal ports bound to 0.0.0.0 | Internal services bound to `127.0.0.1` only |

**Files added:**

| File | Purpose |
|------|---------|
| `config/traefik/traefik.yml` | Static config: entrypoints (80→443 redirect), Docker provider, ACME cert resolver |
| `config/traefik/dynamic/middlewares.yml` | Security headers middleware applied to all public routers |

**Files modified:**

| File | Change |
|------|--------|
| `docker-compose.prod-local.yml` | Added `traefik` service; added Docker labels to `frontend`, `aurora-server`, `chatbot` for Traefik routing; removed host port bindings from public services; bound internal services to `127.0.0.1` |
| `.env.example` | Added `DOMAIN`, `API_DOMAIN`, `WS_DOMAIN`, `ACME_EMAIL` |

**Routing via Docker labels:**

| Service | Domain | Port |
|---------|--------|------|
| `frontend` | `${DOMAIN}` | 3000 |
| `aurora-server` | `${API_DOMAIN}` | `${FLASK_PORT}` |
| `chatbot` | `${WS_DOMAIN}` | 5006 |

**K8s deployments** already use Nginx Ingress Controller with TLS certificates (see `website/docs/deployment/kubernetes.md`). No changes needed, existing Ingress annotations handle TLS termination.

### CORS

| Before | After |
|--------|-------|
| `create_cors_response()` reflects arbitrary `Origin` | Identified as a gap should use `FRONTEND_URL` instead of reflecting |

**File:** `server/utils/web/cors_utils.py:10`

The main CORS middleware in `main_compute.py:117` is correctly strict (`origins=FRONTEND_URL`). The `create_cors_response()` helper is used in preflight OPTIONS handlers across blueprint routes and should be hardened to match.

### Rate Limiting (existing, no changes)

Already implemented via Flask-Limiter with Redis backend:

| Layer | Policy |
|-------|--------|
| Global default | 2000/day, 500/hour |
| Auth endpoints (GCP, AWS, Azure, OVH) | 10/min, 50/hour, 200/day |
| General API | 60/min, 1000/hour, 10000/day |
| Health/metrics/probes | Exempt |
| Localhost (127.0.0.1, ::1) | Exempt |

Configuration: `server/config/rate_limiting.py`
Implementation: `server/utils/web/limiter_ext.py`
Key function: rate limit by `user_id` when authenticated, otherwise by client IP.

### Secrets Management (existing, no changes)

- **HashiCorp Vault** KV v2 engine at the `aurora` mount
- Credentials stored as Vault secret references (`vault:kv/data/aurora/users/{secret_name}`), resolved at runtime never plaintext in DB
- Auto-initialization: `vault-init` container initializes and unseals Vault on startup, keys persisted in Docker volume
- Token management via `server/utils/auth/token_management.py` and `server/utils/secrets/secret_ref_utils.py`

### Database Security (existing, no changes)

- **PostgreSQL Row-Level Security (RLS)** enabled on all tenant-scoped tables
- Session variable `SET myapp.current_user_id` set before every query
- RLS policies enforce that users can only SELECT/INSERT/UPDATE/DELETE their own rows
- Tables with RLS: incidents, postmortems, incident_alerts, incident_feedback, grafana_alerts, datadog_events, netdata_alerts, splunk_alerts, bigpanda_events, jenkins_deployment_events, dynatrace_problems, and more
- Implementation: `server/utils/db/db_utils.py` (lines 901–1539)

### Network Segmentation (Docker Compose prod)

| Service | Before | After |
|---------|--------|-------|
| Frontend | `0.0.0.0:3000` | No host port (Traefik only) |
| API Server | `0.0.0.0:5080` | No host port (Traefik only) |
| WebSocket | `0.0.0.0:5006` | No host port (Traefik only) |
| PostgreSQL | `0.0.0.0:5432` | `127.0.0.1:5432` |
| Redis | `0.0.0.0:6379` | `127.0.0.1:6379` |
| Vault | `0.0.0.0:8200` | `127.0.0.1:8200` |
| Weaviate | `0.0.0.0:8080` | `127.0.0.1:8080` |
| SeaweedFS | `0.0.0.0:8333,8888,9333` | `127.0.0.1:*` |
| Memgraph | `0.0.0.0:7687` | `127.0.0.1:7687` |
| SearXNG | `0.0.0.0:8082` | `127.0.0.1:8082` |

## P.F.P. (Potential Failure Points)

1. **CORS origin reflection**: `create_cors_response()` in `cors_utils.py` still reflects any `Origin` header. This should be fixed to use `FRONTEND_URL` exclusively.

2. **Vault auto-unseal keys in Docker volume**: The `vault-init` container stores unseal keys and root token in the `vault-init` volume. If this volume is compromised, all secrets are exposed. For production: consider Vault auto-unseal with a cloud KMS (AWS KMS, GCP KMS, Azure Key Vault). See `website/docs/deployment/vault-kms-setup.md`.

3. **Rate limiter toggle**: Rate limiting can be disabled entirely via `RATE_LIMITING_ENABLED=false`. Ensure this is always `true` in production.

4. **Docker socket mount**: `aurora-server`, `celery_worker`, and `chatbot` mount `/var/run/docker.sock` for container operations. This grants root-equivalent access to the Docker daemon. Limit this to services that genuinely need it.

5. **Let's Encrypt rate limits**: ACME HTTP-01 challenges require port 80 to be publicly accessible. If behind a firewall or NAT, use DNS-01 challenge instead (requires DNS provider API).

## Technical Architecture

```
                        Internet
                           │
                    ┌──────┴──────┐
                    │   Traefik   │  :80 → :443 redirect
                    │  TLS 1.3    │  Let's Encrypt ACME
                    │  HSTS       │  Security headers
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────┴──┐   ┌─────┴────┐  ┌────┴─────┐
     │ Frontend  │   │   API    │  │ WebSocket│
     │ Next.js   │   │  Flask   │  │ Chatbot  │
     │ :3000     │   │  :5080   │  │  :5006   │
     └─────┬─────┘   └────┬─────┘  └────┬─────┘
           │              │             │
           │         ┌────┴─────────────┘
           │         │
    ┌──────┴─────────┴──────────────────────────────┐
    │              Internal Network                 │
    │         (127.0.0.1 bound only)                │
    │                                               │
    │  ┌──────────┐  ┌───────┐  ┌────────────────┐  │
    │  │PostgreSQL│  │ Redis │  │   Vault KV v2  │  │
    │  │  :5432   │  │ :6379 │  │    :8200       │  │
    │  │   RLS    │  │       │  │  secrets at    │  │
    │  └──────────┘  └───────┘  │  rest          │  │
    │                           └────────────────┘  │
    │  ┌──────────┐  ┌──────────┐  ┌─────────────┐  │
    │  │ Weaviate │  │SeaweedFS │  │  Memgraph   │  │
    │  │  :8080   │  │  :8333   │  │   :7687     │  │
    │  └──────────┘  └──────────┘  └─────────────┘  │
    └───────────────────────────────────────────────┘
```

**Data flow — secrets:**
1. Flask receives authenticated request
2. Resolves `vault:kv/data/aurora/users/{name}` reference via Vault HTTP API
3. Vault returns decrypted credential
4. Flask uses credential to call external API (GCP, AWS, Azure, etc.)
5. Credential never stored in plaintext — only the Vault reference persists in PostgreSQL

**Data flow — rate limiting:**
1. Request arrives at Flask
2. `get_rate_limit_key()` extracts user ID (authenticated) or client IP (anonymous)
3. Flask-Limiter checks Redis counter against configured limits
4. If exceeded → 429 with `Retry-After` header
5. Health probes, localhost, and bypass-token requests are exempt
