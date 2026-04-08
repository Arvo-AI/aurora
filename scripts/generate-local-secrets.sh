#!/bin/bash
# ============================================================================
# Generate Local Secrets for Aurora
# ============================================================================
# This script generates secure random secrets for local Aurora deployment.
# Run this before your first 'make prod-prebuilt' (or make prod-local) to populate required secrets.
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

# Helper: set a secret only if it's empty or missing
set_secret_if_empty() {
    local key="$1"
    local val
    val=$(grep "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    if [ -n "$val" ]; then
        echo -e "  ${key} already set, skipping"
        return
    fi
    local secret
    secret=$(generate_secret)
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i.bak "s|^${key}=.*|${key}=$secret|" "$ENV_FILE"
    else
        echo "${key}=$secret" >> "$ENV_FILE"
    fi
    echo -e "  ${KEY:-$key} generated"
}

set_secret_if_empty POSTGRES_PASSWORD
set_secret_if_empty FLASK_SECRET_KEY
set_secret_if_empty AUTH_SECRET
set_secret_if_empty SEARXNG_SECRET
set_secret_if_empty MEMGRAPH_PASSWORD

# Add AGENT_RECURSION_LIMIT if not present (required for agent)
if ! grep -q "^AGENT_RECURSION_LIMIT=" "$ENV_FILE"; then
    echo "AGENT_RECURSION_LIMIT=240" >> "$ENV_FILE"
fi

# Clean up backup files
rm -f "$ENV_FILE.bak"

echo -e "${GREEN}✓ Secrets generated and saved to .env${NC}"

# ── LLM provider prompt (interactive only) ────────────────────────────────

_existing_llm_key=""
for _k in OPENROUTER_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY GOOGLE_AI_API_KEY; do
  _v=$(grep "^${_k}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
  [ -n "$_v" ] && { _existing_llm_key="$_v"; break; }
done

if [ -n "${LLM_API_KEY:-}" ]; then
  _provider="${LLM_PROVIDER:-openrouter}"
  _key="$LLM_API_KEY"
elif [ -n "$_existing_llm_key" ]; then
  echo -e "${GREEN}✓ LLM key already configured${NC}"
  _provider=""
  _key=""
elif [ -t 0 ]; then
  echo ""
  echo "  LLM Provider Configuration"
  echo "  1) OpenRouter  — one key, many models (recommended)"
  echo "  2) OpenAI"
  echo "  3) Anthropic"
  echo "  4) Google AI"
  echo "  5) Skip — I'll configure it later in .env"
  echo ""
  printf "  Choice [1]: "
  read -r _choice
  _choice="${_choice:-1}"

  _provider=""
  case "$_choice" in
    1) _provider="openrouter" ;;
    2) _provider="openai" ;;
    3) _provider="anthropic" ;;
    4) _provider="google" ;;
    5|*) _provider="" ;;
  esac

  _key=""
  if [ -n "$_provider" ]; then
    printf "  API key: "
    read -r _key
  fi
else
  echo -e "${YELLOW}! No LLM key configured. Set it in .env before starting Aurora.${NC}"
  _provider=""
  _key=""
fi

if [ -n "$_provider" ] && [ -n "$_key" ]; then
  case "$_provider" in
    openrouter) _env_key="OPENROUTER_API_KEY"; _mode="openrouter" ;;
    openai)     _env_key="OPENAI_API_KEY";     _mode="direct" ;;
    anthropic)  _env_key="ANTHROPIC_API_KEY";   _mode="direct" ;;
    google)     _env_key="GOOGLE_AI_API_KEY";   _mode="direct" ;;
  esac
  sed -i.bak "s|^LLM_PROVIDER_MODE=.*|LLM_PROVIDER_MODE=${_mode}|" "$ENV_FILE"
  if grep -q "^${_env_key}=" "$ENV_FILE"; then
    sed -i.bak "s|^${_env_key}=.*|${_env_key}=${_key}|" "$ENV_FILE"
  else
    echo "${_env_key}=${_key}" >> "$ENV_FILE"
  fi
  rm -f "$ENV_FILE.bak"
  echo -e "${GREEN}✓ LLM key saved (${_provider})${NC}"
elif [ -n "$_provider" ] && [ -z "$_key" ]; then
  echo -e "${YELLOW}! No key entered. Add it to .env later.${NC}"
fi

echo ""
