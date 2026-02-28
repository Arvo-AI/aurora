# Aurora

Aurora is an automated root cause analysis investigation tool that uses agents to help Site Reliability Engineers resolve incidents.

**[Documentation](https://arvo-ai.github.io/aurora/)** — full guides for deployment, configuration, and connectors.

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
