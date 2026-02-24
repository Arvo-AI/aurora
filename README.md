# Aurora

Aurora is an automated root cause analysis investigation tool that uses agents to help Site Reliability Engineers resolve incidents.

---

## Try the Demo

Want to see Aurora in action without connecting cloud providers or alert sources? Check out the **demo branch** with a pre-loaded, fully analyzed incident.

```bash
# Clone and checkout the demo branch
git clone https://github.com/arvo-ai/aurora.git
cd aurora
git checkout demo

# Initialize (generates secrets)
make init

# Optional: Add an LLM API key to .env for chat functionality
# Get a key from: https://openrouter.ai/keys or https://platform.openai.com/api-keys
nano .env  # Add OPENROUTER_API_KEY=sk-or-v1-...

# Start Aurora
make dev  # or: make prod-prebuilt for production images

# Get Vault root token and add to .env (see Quick Start below for details)
docker logs vault-init 2>&1 | grep "Root Token:"
nano .env  # Add VAULT_TOKEN=hvs.xxxx

# Restart to load Vault token
make down && make dev
```

**What you'll see:**
- Pre-analyzed incident: "Database connection pool exhausted - payment-service"
- Complete RCA with root cause identified (connection leak in commit 7d6f976c)
- Investigation thoughts showing Aurora's reasoning process
- Actionable suggestions with code fixes
- Interactive chat to ask questions about the incident (requires LLM API key)

Open http://localhost:3000, sign up with any email, and explore!

---

## Quick Start

Get Aurora running locally for testing and evaluation:

```bash
# 1. Clone the repository
git clone https://github.com/arvo-ai/aurora.git
cd aurora

# 2. Initialize configuration (generates secure secrets automatically)
make init

# 3. Edit .env and add your LLM API key
#    Get one from: https://openrouter.ai/keys or https://platform.openai.com/api-keys
nano .env  # Add OPENROUTER_API_KEY=sk-or-v1-...

# 4. Start Aurora (prebuilt from GHCR, or build from source)
make prod-prebuilt   # or: make prod-local to build images locally

# 5. Get Vault root token and add to .env
#    Check the vault-init container logs for the root token:
docker logs vault-init 2>&1 | grep "Root Token:"
#    You'll see output like:
#    ===================================================
#    Vault initialization complete!
#    Root Token: hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
#    IMPORTANT: Set VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx in your .env file
#               to connect Aurora services to Vault.
#    ===================================================
#    Copy the root token value and add it to your .env file:
nano .env  # Add VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 6. Restart Aurora to load the Vault token
make down
make prod-prebuilt   # or: make prod-local to build from source
```

**That's it!** Open http://localhost:3000 in your browser.

### Pin a specific version

By default, `make prod-prebuilt` pulls the latest images. To pin a release:

```bash
make prod-prebuilt VERSION=v1.2.3
```

Available versions are listed at https://github.com/orgs/Arvo-AI/packages.

### Build from source instead

To build images locally (e.g. testing a feature branch):

```bash
make prod-local
```

> **Note**: Aurora works **without any cloud provider accounts**! The LLM API key is the only external requirement. Connectors are optional and can be enabled later if needed via the env file.

## Repo overview
- `server/` Python API, chatbot, Celery workers
- `client/` Next.js frontend
- `docker-compose.yaml` local stack (postgres, redis, weaviate, vault, seaweedfs)



Open http://localhost:3000 (API: http://localhost:5080, Chatbot WS: ws://localhost:5006)

To stop: `make down`  
Logs: `make logs`

If you want cloud connectors, add provider credentials referenced in `.env.example`.

## License
Apache License 2.0. See [LICENSE](LICENSE).
