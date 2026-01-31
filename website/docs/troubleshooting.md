---
sidebar_position: 6
---

# Troubleshooting

Common issues and their solutions.

## Installation Issues

### ".env file not found"

```
Error: .env file not found.
Please run 'make init' first to set up your environment.
```

**Solution**: Run the init command first:

```bash
make init
```

### "Secrets not generated"

```
Error: Secrets not generated. Run 'make init' first.
```

**Solution**: The `.env` file exists but secrets are empty:

```bash
make init
```

### Docker Compose Version Error

**Solution**: Ensure Docker Compose >= 28.x:

```bash
docker compose version
```

If older, update Docker Desktop or install the latest Docker Compose.

## Vault Issues

### "Vault token not set"

**Symptoms**: Services can't connect to Vault, credential errors.

**Solution**:

1. Get the root token:
```bash
docker logs vault-init 2>&1 | grep "Root Token:"
```

2. Add to `.env`:
```bash
VAULT_TOKEN=hvs.xxxxxxxxxxxx
```

3. Restart:
```bash
make down && make prod-local
```

### "Vault is sealed"

**Solution**:

```bash
docker restart vault-init
```

The init container will auto-unseal Vault.

### "Permission denied" on Vault

**Solution**: Your token may lack required policies. Use the root token or check token capabilities:

```bash
docker exec -it vault vault token capabilities aurora/users/
```

## Port Conflicts

### "Port already in use"

**Solution**: Check which process is using the port:

```bash
# macOS/Linux
lsof -i :3000

# Windows
netstat -ano | findstr :3000
```

Either stop the conflicting process or change Aurora's port in docker-compose.

### Required Ports

Ensure these are free:
- 3000 (Frontend)
- 5080 (API)
- 5006 (Chatbot)
- 5432 (PostgreSQL)
- 6379 (Redis)
- 8080 (Weaviate)
- 8200 (Vault)
- 8333 (SeaweedFS)

## Container Issues

### Container Won't Start

```bash
# Check container status
docker compose ps

# View logs for specific container
docker logs aurora-server

# Restart specific container
docker compose restart aurora-server
```

### Out of Memory

**Symptoms**: Containers killed, system slowdown.

**Solution**: Increase Docker memory allocation in Docker Desktop settings (recommend 8GB+).

### Build Failures

```bash
# Clean rebuild
make nuke
make dev
```

## Database Issues

### "Connection refused" to PostgreSQL

1. Check container is running:
```bash
docker ps | grep postgres
```

2. Check logs:
```bash
docker logs aurora-postgres-1
```

3. Verify connection settings in `.env`:
```bash
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
```

### Database Migrations

If schema is out of sync, a full reset may be needed:

```bash
make clean  # Warning: destroys data
make dev
```

## LLM Issues

### "Invalid API key"

1. Check key format matches provider:
   - OpenRouter: `sk-or-v1-...`
   - OpenAI: `sk-...`
   - Anthropic: `sk-ant-...`

2. Verify key is active in provider dashboard

3. Check correct variable name in `.env`

### "Rate limit exceeded"

- Wait 60 seconds and retry
- Check provider rate limits
- Consider upgrading API tier

## Frontend Issues

### White Screen / Loading Forever

1. Check browser console for errors
2. Verify API is reachable:
```bash
curl http://localhost:5080/health
```

3. Check frontend logs:
```bash
docker logs aurora-frontend
```

### Authentication Errors

1. Verify `AUTH_SECRET` is set in `.env`
2. Check `NEXTAUTH_URL` matches your URL
3. Restart frontend:
```bash
docker compose restart frontend
```

## Getting Help

If these solutions don't work:

1. Check [GitHub Issues](https://github.com/arvo-ai/aurora/issues)
2. Open a new issue with:
   - Error message
   - Steps to reproduce
   - Docker/OS versions
   - Relevant logs

3. Contact: info@arvoai.ca
