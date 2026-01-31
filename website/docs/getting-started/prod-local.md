---
sidebar_position: 3
---

# Production-Local Mode

Run a production-like Aurora stack locally for testing and evaluation.

## Overview

`prod-local` mode uses production Docker images but runs entirely on your machine. This is ideal for:

- Testing before deploying to production
- Evaluating Aurora without cloud infrastructure
- Demo environments

## Commands

```bash
# First-time setup
make init

# Start production-local
make prod-local

# View logs
make prod-local-logs

# Stop
make prod-local-down

# Clean (removes volumes, preserves .env)
make prod-local-clean

# Full cleanup
make prod-local-nuke
```

## Vault Setup

Vault automatically initializes on first startup. The root token is stored in the `vault-init` container logs.

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
# Add: VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Restart to Apply

```bash
make down
make prod-local
```

## Service Endpoints

| Service | URL | Description |
|---------|-----|-------------|
| Frontend | http://localhost:3000 | Web UI |
| Backend API | http://localhost:5080 | REST API |
| Chatbot | ws://localhost:5006 | WebSocket |
| Vault UI | http://localhost:8200 | Secrets management |
| SeaweedFS | http://localhost:8333 | S3-compatible storage |

## Testing Vault

Verify Vault is working:

```bash
# Write a test secret
docker exec -it vault vault kv put aurora/users/test-secret value='hello'

# Read it back
docker exec -it vault vault kv get aurora/users/test-secret
```

## Differences from Development

| Aspect | `make dev` | `make prod-local` |
|--------|------------|-------------------|
| Docker images | Development | Production |
| Hot reload | Enabled | Disabled |
| Optimizations | Off | On |
| Use case | Contributing | Testing/Eval |

## Troubleshooting

### "Secrets not generated" Error

```bash
make init
make prod-local
```

### Vault Connection Issues

1. Check Vault is running: `docker ps | grep vault`
2. Check token is in `.env`: `grep VAULT_TOKEN .env`
3. Restart: `make down && make prod-local`

### Port Conflicts

Ensure these ports are free: 3000, 5080, 5006, 5432, 6379, 8080, 8200, 8333

## Next Steps

- [Environment Configuration](/docs/configuration/environment) - All settings
- [Vault Configuration](/docs/configuration/vault) - Secrets management details
- [Connectors](/docs/integrations/connectors) - Add cloud providers
