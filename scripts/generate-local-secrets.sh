#!/bin/bash
# ============================================================================
# Generate Local Secrets for Aurora
# ============================================================================
# This script generates secure random secrets for local Aurora deployment.
# Run this before your first 'make prod-local' to populate required secrets.
#
# Usage:
#   ./scripts/generate-local-secrets.sh
#   # Or let make init run it automatically
# ============================================================================

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Generating secure secrets for Aurora...${NC}"

# Check if .env file exists
ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}Warning: .env file not found. Creating from .env.example...${NC}"
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        echo "Error: .env.example not found. Please create .env manually."
        exit 1
    fi
fi

# Function to generate a random 32-character secret
generate_secret() {
    if command -v openssl &> /dev/null; then
        openssl rand -hex 32
    elif command -v python3 &> /dev/null; then
        python3 -c "import secrets; print(secrets.token_hex(32))"
    else
        # Fallback: use /dev/urandom
        cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 64 | head -n 1
    fi
}

# Generate secrets
POSTGRES_PASSWORD=$(generate_secret)
FLASK_SECRET_KEY=$(generate_secret)
AUTH_SECRET=$(generate_secret)

# Update .env file
# Use sed to update or add each variable
if grep -q "^POSTGRES_PASSWORD=" "$ENV_FILE"; then
    sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$POSTGRES_PASSWORD|" "$ENV_FILE"
else
    echo "POSTGRES_PASSWORD=$POSTGRES_PASSWORD" >> "$ENV_FILE"
fi

if grep -q "^FLASK_SECRET_KEY=" "$ENV_FILE"; then
    sed -i.bak "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$FLASK_SECRET_KEY|" "$ENV_FILE"
else
    echo "FLASK_SECRET_KEY=$FLASK_SECRET_KEY" >> "$ENV_FILE"
fi

if grep -q "^AUTH_SECRET=" "$ENV_FILE"; then
    sed -i.bak "s|^AUTH_SECRET=.*|AUTH_SECRET=$AUTH_SECRET|" "$ENV_FILE"
else
    echo "AUTH_SECRET=$AUTH_SECRET" >> "$ENV_FILE"
fi

# Add AGENT_RECURSION_LIMIT if not present (required for agent)
if ! grep -q "^AGENT_RECURSION_LIMIT=" "$ENV_FILE"; then
    echo "AGENT_RECURSION_LIMIT=240" >> "$ENV_FILE"
fi

# Clean up backup files
rm -f "$ENV_FILE.bak"

echo -e "${GREEN}✓ Secrets generated and saved to .env${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your LLM API key (OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)"
echo "  2. Run: make prod-local"
echo ""
