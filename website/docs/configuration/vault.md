---
sidebar_position: 2
---

# Vault Configuration

Aurora uses HashiCorp Vault for secrets management. User credentials (cloud provider tokens, API keys) are stored in Vault rather than directly in the database.

## How It Works

- **Persistent storage**: Vault uses file-based storage with data persisted in Docker volumes (`vault-data`, `vault-init`)
- **Auto-initialization**: The `vault-init` container automatically initializes and unseals Vault on startup
- **Secret references**: Stored in the database as `vault:kv/data/aurora/users/{secret_name}`, resolved at runtime

## First-Time Setup

### 1. Start Aurora

```bash
make prod-local
```

### 2. Get the Root Token

```bash
docker logs vault-init 2>&1 | grep "Root Token:"
```

Output:

```
===================================================
Vault initialization complete!
Root Token: hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
IMPORTANT: Set VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx in your .env file
===================================================
```

### 3. Add to .env

```bash
VAULT_TOKEN=hvs.xxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 4. Restart Aurora

```bash
make down
make prod-local
```

## Configuration Variables

```bash
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=hvs.xxxxxxxxxxxx
VAULT_KV_MOUNT=aurora
VAULT_KV_BASE_PATH=users
```

## Testing Vault

### Via CLI

```bash
# Write a secret
docker exec -it vault vault kv put aurora/users/test-secret value='hello'

# Read a secret
docker exec -it vault vault kv get aurora/users/test-secret

# List secrets
docker exec -it vault vault kv list aurora/users/
```

### Via UI

1. Open http://localhost:8200
2. Sign in with your root token
3. Navigate to `aurora/users/`

## Secret Storage Pattern

Aurora stores user credentials using this reference format:

```
vault:kv/data/aurora/users/{user_id}/{credential_type}
```

The application resolves these references at runtime, never storing raw credentials in the database.

## Vault Persistence

Data is stored in Docker volumes:

| Volume | Purpose |
|--------|---------|
| `vault-data` | Encrypted secret storage |
| `vault-init` | Initialization keys and tokens |

These persist across container restarts. Use `make prod-local-clean` to remove them.

## Production Considerations

For production deployments:

1. **Don't use the root token** - Create limited-access tokens
2. **Enable audit logging** - Track secret access
3. **Use auto-unseal** - Consider cloud KMS for unsealing
4. **Backup regularly** - The `vault-data` volume contains all secrets

## Troubleshooting

### "Vault is sealed"

The `vault-init` container should auto-unseal. If not:

```bash
docker restart vault-init
```

### "Permission denied"

Check your token has the correct policies:

```bash
docker exec -it vault vault token capabilities aurora/users/
```

### Connection refused

Ensure Vault is running:

```bash
docker ps | grep vault
curl http://localhost:8200/v1/sys/health
```
