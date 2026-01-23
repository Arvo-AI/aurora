#!/bin/bash
# ============================================================================
# Initialize Vault for Production-Local Mode
# ============================================================================
# This script initializes Vault with file storage (production-like mode).
# It's called automatically by 'make init'.
#
# Usage:
#   ./scripts/init-prod-vault.sh
# ============================================================================

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}Initializing Vault for production-local mode...${NC}"

# Check if Vault container is running
if ! docker ps | grep -q aurora-vault; then
    echo -e "${YELLOW}Vault container not running. Starting it first...${NC}"
    docker compose -f docker-compose.prod-local.yml up -d vault
    echo "Waiting for Vault to be ready..."
    sleep 10
fi

# Wait for Vault to be ready
MAX_RETRIES=30
RETRY_COUNT=0
while ! docker exec aurora-vault vault status &> /dev/null; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    if [ $RETRY_COUNT -ge $MAX_RETRIES ]; then
        echo -e "${RED}Error: Vault did not become ready in time${NC}"
        exit 1
    fi
    echo "Waiting for Vault to be ready... ($RETRY_COUNT/$MAX_RETRIES)"
    sleep 2
done

echo -e "${GREEN}✓ Vault is ready${NC}"

# Check if Vault is already initialized
if docker exec aurora-vault vault status 2>&1 | grep -q "Initialized.*true"; then
    echo -e "${YELLOW}Vault is already initialized${NC}"
    
    # Check if KV engine is enabled
    if docker exec aurora-vault vault secrets list 2>&1 | grep -q "aurora/"; then
        echo -e "${GREEN}✓ KV engine already enabled at 'aurora'${NC}"
        exit 0
    fi
fi

# Initialize Vault if not already initialized
if ! docker exec aurora-vault vault status 2>&1 | grep -q "Initialized.*true"; then
    echo "Initializing Vault..."
    docker exec aurora-vault vault operator init \
        -key-shares=1 \
        -key-threshold=1 \
        -format=json > /tmp/vault-init.json
    
    UNSEAL_KEY=$(cat /tmp/vault-init.json | jq -r '.unseal_keys_b64[0]')
    ROOT_TOKEN=$(cat /tmp/vault-init.json | jq -r '.root_token')
    
    echo "Unsealing Vault..."
    docker exec aurora-vault vault operator unseal "$UNSEAL_KEY"
    
    echo "Authenticating with root token..."
    docker exec -e VAULT_TOKEN="$ROOT_TOKEN" aurora-vault vault auth "$ROOT_TOKEN"
    
    echo "Enabling KV v2 secrets engine..."
    docker exec -e VAULT_TOKEN="$ROOT_TOKEN" aurora-vault vault secrets enable -path=aurora kv-v2 || true
    
    # Save root token to .env if VAULT_TOKEN is not set
    if ! grep -q "^VAULT_TOKEN=" .env 2>/dev/null || grep -q "^VAULT_TOKEN=$" .env 2>/dev/null; then
        if [ -f .env ]; then
            if grep -q "^VAULT_TOKEN=" .env; then
                sed -i.bak "s|^VAULT_TOKEN=.*|VAULT_TOKEN=$ROOT_TOKEN|" .env
            else
                echo "VAULT_TOKEN=$ROOT_TOKEN" >> .env
            fi
            rm -f .env.bak
            echo -e "${GREEN}✓ Vault token saved to .env${NC}"
        fi
    fi
    
    rm -f /tmp/vault-init.json
    echo -e "${GREEN}✓ Vault initialized successfully${NC}"
else
    echo -e "${YELLOW}Vault already initialized, enabling KV engine...${NC}"
    
    # Get root token from .env
    if [ -f .env ]; then
        ROOT_TOKEN=$(grep "^VAULT_TOKEN=" .env | cut -d '=' -f2 | tr -d '"' | tr -d "'")
        if [ -z "$ROOT_TOKEN" ]; then
            echo -e "${RED}Error: VAULT_TOKEN not found in .env${NC}"
            exit 1
        fi
    else
        echo -e "${RED}Error: .env file not found${NC}"
        exit 1
    fi
    
    # Enable KV engine if not already enabled
    if ! docker exec -e VAULT_TOKEN="$ROOT_TOKEN" aurora-vault vault secrets list 2>&1 | grep -q "aurora/"; then
        docker exec -e VAULT_TOKEN="$ROOT_TOKEN" aurora-vault vault secrets enable -path=aurora kv-v2
        echo -e "${GREEN}✓ KV engine enabled${NC}"
    else
        echo -e "${GREEN}✓ KV engine already enabled${NC}"
    fi
fi

echo ""
echo -e "${GREEN}Vault initialization complete!${NC}"
