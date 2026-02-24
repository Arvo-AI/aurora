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
- **Backend** (server/): Flask REST API (main_compute.py), WebSocket chatbot (main_chatbot.py), Celery tasks, connectors for GCP/AWS/Azure, LangGraph agent workflow
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
