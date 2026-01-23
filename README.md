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
```

**That's it!** Open http://localhost:3000 in your browser.

📖 **Full Quick Start Guide**: See [docs/QUICK_START.md](./docs/QUICK_START.md) for detailed instructions and troubleshooting.

> **Note**: Aurora works **without any cloud provider accounts**! The LLM API key is the only external requirement. Cloud connectors (GCP, AWS, Azure) are optional and can be enabled later if needed.

## Architecture

## Repo overview
- `server/` Python API, chatbot, Celery workers
- `client/` Next.js frontend
- `docker-compose.yaml` local stack (postgres, redis, weaviate, vault, seaweedfs)

## Quickstart
Requirements: Docker with the Compose plugin.

```bash
cp .env.example .env
make dev
```

Open http://localhost:3000 (API: http://localhost:5080, Chatbot WS: ws://localhost:5006)

To stop: `make down`  
Logs: `make logs`

If you want cloud connectors, add provider credentials referenced in `.env.example`.

## License
Apache License 2.0. See [LICENSE](LICENSE).
