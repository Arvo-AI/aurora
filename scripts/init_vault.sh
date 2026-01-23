#!/bin/bash
#
# Initialize HashiCorp Vault for Aurora development
#
# This script:
# 1. Waits for Vault to be ready
# 2. Enables the KV v2 secrets engine at 'aurora' mount
# 3. Creates a basic policy for the aurora application
#
# Usage:
#   ./scripts/init_vault.sh
#
# Environment variables:
#   VAULT_ADDR  - Vault server address (default: http://127.0.0.1:8200)
#   VAULT_TOKEN - Root token for initialization (default: dev-token)
#

set -e

# Configuration
VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-dev-token}"
VAULT_KV_MOUNT="${VAULT_KV_MOUNT:-aurora}"
MAX_RETRIES=30
RETRY_INTERVAL=2

echo "==================================================="
echo "Aurora Vault Initialization Script"
echo "==================================================="
echo "VAULT_ADDR: $VAULT_ADDR"
echo "KV Mount: $VAULT_KV_MOUNT"
echo ""

# Export for vault CLI
export VAULT_ADDR
export VAULT_TOKEN

# Wait for Vault to be ready
echo "Waiting for Vault to be ready..."
retries=0
until vault status > /dev/null 2>&1; do
    retries=$((retries + 1))
    if [ $retries -ge $MAX_RETRIES ]; then
        echo "ERROR: Vault did not become ready after $MAX_RETRIES attempts"
        exit 1
    fi
    echo "  Attempt $retries/$MAX_RETRIES - Vault not ready, waiting ${RETRY_INTERVAL}s..."
    sleep $RETRY_INTERVAL
done

echo "Vault is ready!"
echo ""

# Check if KV engine is already enabled
echo "Checking if KV engine is already enabled at '$VAULT_KV_MOUNT'..."
if vault secrets list | grep -q "^${VAULT_KV_MOUNT}/"; then
    echo "  KV engine already enabled at '$VAULT_KV_MOUNT'"
else
    echo "  Enabling KV v2 secrets engine at '$VAULT_KV_MOUNT'..."
    vault secrets enable -path="$VAULT_KV_MOUNT" kv-v2
    echo "  KV v2 secrets engine enabled successfully"
fi
echo ""

# Create a policy for Aurora application
echo "Creating Aurora policy..."
vault policy write aurora-app - <<EOF
# Aurora application policy
# Allows full CRUD on the aurora KV mount

# Allow all operations on user secrets
path "${VAULT_KV_MOUNT}/data/users/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "${VAULT_KV_MOUNT}/metadata/users/*" {
  capabilities = ["list", "read", "delete"]
}

# Allow listing secrets
path "${VAULT_KV_MOUNT}/metadata/" {
  capabilities = ["list"]
}

path "${VAULT_KV_MOUNT}/metadata/users" {
  capabilities = ["list"]
}
EOF
echo "  Aurora policy created"
echo ""

# Verify setup
echo "Verifying setup..."
echo "  Secrets engines:"
vault secrets list | grep -E "^(Path|${VAULT_KV_MOUNT})" || true
echo ""
echo "  Policies:"
vault policy list | grep aurora || true
echo ""

echo "==================================================="
echo "Vault initialization complete!"
echo ""
echo "Quick test commands:"
echo "  # Store a test secret"
echo "  vault kv put $VAULT_KV_MOUNT/users/test-secret value='hello-world'"
echo ""
echo "  # Read it back"
echo "  vault kv get $VAULT_KV_MOUNT/users/test-secret"
echo ""
echo "  # List secrets"
echo "  vault kv list $VAULT_KV_MOUNT/users/"
echo "==================================================="
