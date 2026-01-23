# Aurora

Aurora is an automated root cause analysis investigation tool that uses agents to help Site Reliability Engineers resolve incidents.

## 🚀 Quick Start (5 Minutes)

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

# 4. Start Aurora
make prod-local

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
make prod-local
```

**That's it!** Open http://localhost:3000 in your browser.


> **Note**: Aurora works **without any cloud provider accounts**! The LLM API key is the only external requirement. Cloud connectors (GCP, AWS, Azure) are optional and can be enabled later if needed.

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
