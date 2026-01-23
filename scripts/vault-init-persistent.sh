#!/bin/sh
#
# Vault initialization and unseal script for persistent storage mode
#
# This script handles:
# 1. Waiting for Vault to be ready
# 2. Initializing Vault if not already initialized
# 3. Unsealing Vault using stored keys
# 4. Enabling KV secrets engine
# 5. Creating Aurora policy
#
# Keys are stored in /vault/init/keys.json for auto-unseal on restart.
# WARNING: This is for development only. Production should use auto-unseal

set -e

VAULT_ADDR="${VAULT_ADDR:-http://vault:8200}"
VAULT_KV_MOUNT="${VAULT_KV_MOUNT:-aurora}"
INIT_FILE="/vault/init/keys.json"
MAX_RETRIES=60
RETRY_INTERVAL=2

export VAULT_ADDR

echo "==================================================="
echo "Vault Initialization Script (Persistent Storage)"
echo "==================================================="
echo "VAULT_ADDR: $VAULT_ADDR"
echo "KV Mount: $VAULT_KV_MOUNT"
echo ""

# Wait for Vault to be available (not necessarily initialized)
echo "Waiting for Vault to be available..."
retries=0
until wget -q -O /dev/null "$VAULT_ADDR/v1/sys/health" 2>/dev/null || wget -q -O /dev/null "$VAULT_ADDR/v1/sys/seal-status" 2>/dev/null; do
    retries=$((retries + 1))
    if [ $retries -ge $MAX_RETRIES ]; then
        echo "ERROR: Vault did not become available after $MAX_RETRIES attempts"
        exit 1
    fi
    echo "  Attempt $retries/$MAX_RETRIES - Vault not available, waiting ${RETRY_INTERVAL}s..."
    sleep $RETRY_INTERVAL
done

echo "Vault is available!"
echo ""

# Check initialization status
echo "Checking Vault initialization status..."
INIT_STATUS=$(wget -q -O - "$VAULT_ADDR/v1/sys/init" 2>/dev/null | grep -o '"initialized":[^,}]*' | cut -d: -f2)

if [ "$INIT_STATUS" = "false" ]; then
    echo "Vault is not initialized. Initializing..."
    
    # Initialize Vault with 1 key share and 1 threshold for simplicity in dev
    INIT_RESPONSE=$(wget -q -O - --post-data='{"secret_shares": 1, "secret_threshold": 1}' \
        --header="Content-Type: application/json" \
        "$VAULT_ADDR/v1/sys/init" 2>/dev/null)
    
    if [ -z "$INIT_RESPONSE" ]; then
        echo "ERROR: Failed to initialize Vault"
        exit 1
    fi
    
    # Save the init response
    mkdir -p /vault/init
    echo "$INIT_RESPONSE" > "$INIT_FILE"
    echo "  Vault initialized. Keys saved to $INIT_FILE"
    
    # Extract root token and unseal key
    ROOT_TOKEN=$(echo "$INIT_RESPONSE" | grep -o '"root_token":"[^"]*"' | cut -d'"' -f4)
    UNSEAL_KEY=$(echo "$INIT_RESPONSE" | grep -o '"keys":\["[^"]*"' | cut -d'"' -f4)
    
    echo "  Root token extracted"
    echo "  Unseal key extracted"
else
    echo "Vault is already initialized"
    
    # Load existing keys
    if [ -f "$INIT_FILE" ]; then
        ROOT_TOKEN=$(cat "$INIT_FILE" | grep -o '"root_token":"[^"]*"' | cut -d'"' -f4)
        UNSEAL_KEY=$(cat "$INIT_FILE" | grep -o '"keys":\["[^"]*"' | cut -d'"' -f4)
        echo "  Loaded keys from $INIT_FILE"
    else
        echo "ERROR: Vault is initialized but no keys file found at $INIT_FILE"
        echo "       You may need to manually provide the unseal key and root token"
        exit 1
    fi
fi

echo ""

# Check seal status and unseal if needed
echo "Checking Vault seal status..."
SEAL_STATUS=$(wget -q -O - "$VAULT_ADDR/v1/sys/seal-status" 2>/dev/null | grep -o '"sealed":[^,}]*' | cut -d: -f2)

if [ "$SEAL_STATUS" = "true" ]; then
    echo "Vault is sealed. Unsealing..."
    
    UNSEAL_RESPONSE=$(wget -q -O - --post-data="{\"key\": \"$UNSEAL_KEY\"}" \
        --header="Content-Type: application/json" \
        "$VAULT_ADDR/v1/sys/unseal" 2>/dev/null)
    
    NEW_SEAL_STATUS=$(echo "$UNSEAL_RESPONSE" | grep -o '"sealed":[^,}]*' | cut -d: -f2)
    
    if [ "$NEW_SEAL_STATUS" = "false" ]; then
        echo "  Vault unsealed successfully!"
    else
        echo "ERROR: Failed to unseal Vault"
        exit 1
    fi
else
    echo "Vault is already unsealed"
fi

echo ""
export VAULT_TOKEN="$ROOT_TOKEN"

# Wait for Vault to be fully ready after unseal
echo "Waiting for Vault to be fully ready..."
retries=0
until wget -q -O /dev/null --header="X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/sys/health" 2>/dev/null; do
    retries=$((retries + 1))
    if [ $retries -ge 30 ]; then
        echo "ERROR: Vault did not become ready after unseal"
        exit 1
    fi
    sleep 1
done
echo "Vault is ready!"
echo ""

# Check if KV engine is already enabled
echo "Checking if KV engine is enabled at '$VAULT_KV_MOUNT'..."
MOUNTS=$(wget -q -O - --header="X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/sys/mounts" 2>/dev/null)

if echo "$MOUNTS" | grep -q "\"${VAULT_KV_MOUNT}/\""; then
    echo "  KV engine already enabled at '$VAULT_KV_MOUNT'"
else
    echo "  Enabling KV v2 secrets engine at '$VAULT_KV_MOUNT'..."
    wget -q -O /dev/null --post-data='{"type": "kv-v2"}' \
        --header="Content-Type: application/json" \
        --header="X-Vault-Token: $VAULT_TOKEN" \
        "$VAULT_ADDR/v1/sys/mounts/$VAULT_KV_MOUNT" 2>/dev/null
    echo "  KV v2 secrets engine enabled"
fi

echo ""

# Create Aurora policy
echo "Creating Aurora policy..."
POLICY_DATA=$(cat <<EOF
{
  "policy": "# Aurora application policy\n# Allows full CRUD on the aurora KV mount\n\n# Allow all operations on user secrets\npath \"${VAULT_KV_MOUNT}/data/users/*\" {\n  capabilities = [\"create\", \"read\", \"update\", \"delete\", \"list\"]\n}\n\npath \"${VAULT_KV_MOUNT}/metadata/users/*\" {\n  capabilities = [\"list\", \"read\", \"delete\"]\n}\n\n# Allow listing secrets\npath \"${VAULT_KV_MOUNT}/metadata/\" {\n  capabilities = [\"list\"]\n}\n\npath \"${VAULT_KV_MOUNT}/metadata/users\" {\n  capabilities = [\"list\"]\n}"
}
EOF
)

if wget -q -O /dev/null --post-data="$POLICY_DATA" \
    --header="Content-Type: application/json" \
    --header="X-Vault-Token: $VAULT_TOKEN" \
    "$VAULT_ADDR/v1/sys/policies/acl/aurora-app" 2>/dev/null; then
    echo "  Aurora policy created"
else
    echo "  WARNING: Failed to create Aurora policy (may already exist)"
fi

echo ""
echo "==================================================="
echo "Vault initialization complete!"
echo ""
echo "Root Token: $ROOT_TOKEN"
echo ""
echo "IMPORTANT: Set VAULT_TOKEN=$ROOT_TOKEN in your .env file"
echo "           to connect Aurora services to Vault."
echo ""
echo "The root token is saved in $INIT_FILE inside the vault-init volume."
echo "For production, use proper secrets management for the root token."
echo "==================================================="
