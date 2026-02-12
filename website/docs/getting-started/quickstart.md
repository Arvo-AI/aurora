---
sidebar_position: 1
---

# Quickstart

Get Aurora running locally in about 5 minutes. This guide covers the fastest path to a working installation.

## Prerequisites

Before you begin, ensure you have:

### Required Software

| Software | Version | Check Command |
|----------|---------|---------------|
| Docker | 24.0+ | `docker --version` |
| Docker Compose | 2.20+ (v2) | `docker compose version` |
| Make | Any | `make --version` |

:::info Docker Compose V2
Aurora requires Docker Compose V2 (the `docker compose` command, not `docker-compose`). This is included by default in Docker Desktop 4.0+.
:::

### System Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM | 8 GB | 16 GB |
| Disk | 10 GB free | 20 GB free |
| CPU | 4 cores | 8 cores |

### LLM API Key

You need at least one LLM provider API key:

| Provider | Get API Key | Notes |
|----------|-------------|-------|
| **OpenRouter** (recommended) | [openrouter.ai/keys](https://openrouter.ai/keys) | Access to multiple models, pay-per-use |
| OpenAI | [platform.openai.com](https://platform.openai.com/api-keys) | GPT Models |
| Anthropic | [console.anthropic.com](https://console.anthropic.com/) | Claude models |
| Google AI | [ai.google.dev](https://ai.google.dev/) | Gemini models |

:::tip Why OpenRouter?
OpenRouter gives you access to multiple LLM providers (OpenAI, Anthropic, Google, Meta, etc.) through a single API key with pay-per-use pricing. No monthly commitments required.
:::

## Step 1: Clone the Repository

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
```

## Step 2: Initialize Configuration

Run the initialization script to generate secure secrets and create your `.env` file:

```bash
make init
```

This command:
- Creates `.env` from `.env.example`
- Generates a secure 64-character `POSTGRES_PASSWORD`
- Generates a secure 64-character `FLASK_SECRET_KEY`
- Generates a secure 64-character `AUTH_SECRET`
- Sets `AGENT_RECURSION_LIMIT=240`

## Step 3: Add Your LLM API Key

Edit the `.env` file and add your LLM provider API key:

```bash
nano .env
# Or use your preferred editor: code .env, vim .env, etc.
```

Add one of these (Openrouter or Claude API key for RCA):

```bash
# Option 1: OpenRouter (recommended)
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Option 2: OpenAI
OPENAI_API_KEY=sk-your-key-here

# Option 3: Anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Option 4: Google AI
GOOGLE_AI_API_KEY=your-key-here
```

## Step 4: Start Aurora

```bash
make prod-prebuilt   # or: make prod-local to build from source
```

This pulls prebuilt images from GHCR and starts all containers (or use `make prod-local` to build images locally). First run takes a few minutes to pull images.

You'll see output like:
```
Starting Aurora in production mode (prebuilt images)...
✓ Aurora is starting! Services will be available at:
  - Frontend: http://localhost:3000
  - Backend API: http://localhost:5080
  - Chatbot WebSocket: ws://localhost:5006
  - Vault UI: http://localhost:8200
```

## Step 5: Configure Vault Token

On first startup, Vault auto-initializes and generates a root token. You need to add this to your `.env`:

### Get the Root Token

```bash
docker logs vault-init 2>&1 | grep "Root Token:"
```

You'll see output like:
```
===================================================
Vault initialization complete!
Root Token: hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
IMPORTANT: Set VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx in your .env file
===================================================
```

### Add Token to .env

```bash
nano .env
```

Add the line:
```bash
VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Restart to Apply

```bash
make down
make prod-prebuilt   # or: make prod-local to build from source
```

## Step 6: Access Aurora

Open your browser to **http://localhost:3000**

You should see the Aurora login page. Create an account to get started.

## Verify Installation

Check all services are running:

```bash
docker compose -f docker-compose.prod-local.yml ps
```

Expected output shows all containers as "running":
```
NAME                    STATUS
aurora-server           running
aurora-celery_worker    running
aurora-chatbot          running
aurora-frontend         running
postgres                running
redis                   running
weaviate                running
vault                   running
seaweedfs-master        running
seaweedfs-filer         running
```

### Health Checks

```bash
# API health
curl http://localhost:5080/health

# Vault health
curl http://localhost:8200/v1/sys/health
```

## Common Commands

| Command | Description |
|---------|-------------|
| `make prod-prebuilt` | Start Aurora (pull images from GHCR) |
| `make prod-local` | Build from source and start Aurora |
| `make down` | Stop Aurora |
| `make prod-logs` | View all logs |
| `make prod-logs aurora-server` | View specific service logs |
| `make prod-clean` | Stop and remove data volumes |

## What's Running

Aurora starts these services:

| Service | Port | Description |
|---------|------|-------------|
| Frontend | 3000 | Next.js web application |
| Backend API | 5080 | Flask REST API |
| Chatbot | 5006 | WebSocket server for real-time chat |
| PostgreSQL | 5432 | Primary database |
| Redis | 6379 | Task queue and cache |
| Weaviate | 8080 | Vector database for semantic search |
| Vault | 8200 | Secrets management |
| SeaweedFS | 8333 | S3-compatible object storage |

## Troubleshooting

### "Error: .env file not found"

Run the init command first:
```bash
make init
```

### "Secrets not generated"

The `.env` file exists but secrets are empty. Re-run init:
```bash
make init
```

### Port Already in Use

Check what's using the port:
```bash
# macOS/Linux
lsof -i :3000

# Stop the conflicting process or change Aurora's port
```

### Vault Token Issues

If services can't connect to Vault:
1. Verify token is in `.env`: `grep VAULT_TOKEN .env`
2. Restart services: `make down && make prod-prebuilt` (or `make prod-local`)

### Container Crashes

Check logs for the failing container:
```bash
docker logs aurora-server
docker logs aurora-celery_worker-1
```

## Next Steps

- [Development Setup](/docs/getting-started/dev-setup) — For contributing to Aurora
- [Configuration Reference](/docs/configuration/environment) — All environment variables
- [Connect Cloud Providers](/docs/integrations/connectors) — Add GCP, AWS, Azure
- [Architecture Overview](/docs/architecture/overview) — Understand how Aurora works

:::info No Cloud Accounts Needed
Aurora works without any cloud provider accounts. The LLM API key is the only external requirement. Cloud connectors (GCP, AWS, Azure) are optional and can be added later.
:::
