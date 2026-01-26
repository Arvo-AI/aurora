#!/bin/bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Environment Variables Validation${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

# Track validation status
HAS_ERRORS=0
HAS_WARNINGS=0

# Temporary files for analysis
USED_VARS_FILE=$(mktemp)
ENV_EXAMPLE_VARS_FILE=$(mktemp)
DOCKER_COMPOSE_VARS_FILE=$(mktemp)

# Cleanup on exit
trap "rm -f $USED_VARS_FILE $ENV_EXAMPLE_VARS_FILE $DOCKER_COMPOSE_VARS_FILE" EXIT

echo -e "${YELLOW}Step 1: Extracting environment variables used in code...${NC}"

# Extract env vars from Python code (backend)
# Patterns: os.getenv("VAR"), os.environ.get("VAR"), os.environ["VAR"]
find server/ -type f -name "*.py" -exec grep -oh \
  -e 'os\.getenv([^)]*' \
  -e 'os\.environ\.get([^)]*' \
  -e 'os\.environ\[[^]]*' \
  {} + 2>/dev/null | \
  grep -oE '"[A-Z_][A-Z0-9_]*"|'"'"'[A-Z_][A-Z0-9_]*'"'"'' | \
  tr -d '"'"'" | \
  sort -u >> "$USED_VARS_FILE" || true

# Extract env vars from TypeScript/JavaScript code (frontend)
# Patterns: process.env.VAR, process.env["VAR"]
find client/ -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) -exec grep -oh \
  -e 'process\.env\.[A-Z_][A-Z0-9_]*' \
  -e 'process\.env\[[^]]*' \
  {} + 2>/dev/null | \
  sed -E 's/process\.env\.([A-Z_][A-Z0-9_]*).*/\1/' | \
  grep -oE '[A-Z_][A-Z0-9_]*' | \
  sort -u >> "$USED_VARS_FILE" || true

# Remove duplicates and common non-env vars
sort -u "$USED_VARS_FILE" -o "$USED_VARS_FILE"

# Filter out common false positives (framework/system variables)
# Note: Use grep -v for better cross-platform compatibility
grep -v -E '^(__NEXT_|NEXT_PHASE$|NEXT_DEBUG_BUILD$|NEXT_PRIVATE_|NEXT_SSG_FETCH_METRICS$|NEXT_SERVER_ACTIONS_ENCRYPTION_KEY$)' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Vercel deployment vars (only exist in Vercel environment)
grep -v '^VERCEL' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# System/Shell variables
grep -v -E '^(NODE_ENV|PATH|HOME|USER|PWD|PORT)$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Build/Test environment variables
grep -v -E '^(DEBUG|ANALYZE|PLAYWRIGHT_BASE_URL|GITHUB_ACTIONS|CI)$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Cloud provider SDK internals (set automatically by SDKs or dynamically at runtime)
grep -v -E '^(CLOUDSDK_AUTH_ACCESS_TOKEN|CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT|CLOUDSDK_IMPERSONATE_SERVICE_ACCOUNT|GOOGLE_OAUTH_ACCESS_TOKEN|GOOGLE_CLOUD_PROJECT|GCLOUD_PROJECT|GOOGLE_APPLICATION_CREDENTIALS|AWS_SESSION_TOKEN|AWS_REGION|AWS_CONFIG_FILE|AWS_SHARED_CREDENTIALS_FILE|ARM_SUBSCRIPTION_ID|ARM_TENANT_ID|ARM_CLIENT_ID|ARM_CLIENT_SECRET|AZURE_AD_TENANT_ID|AZURE_TENANT_ID|AZURE_CLIENT_ID|AZURE_CLIENT_SECRET|TF_CLI_CONFIG_FILE)$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Single letter variables (likely false positives)
grep -v '^N$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# K8s runtime variables (set dynamically in k8s deployments, not in .env)
grep -v -E '^(TERMINAL_|POSTGRES_|USE_UNTRUSTED_NODES)' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Secrets injected via CI/CD (not in .env, set via GitHub secrets and --set-file)
grep -v '^AURORA_SERVICE_ACCOUNT_JSON$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# kubectl Agent variables (injected by Helm in customer clusters, not in .env)
grep -v -E '^(AURORA_AGENT_TOKEN|AGENT_VERSION)$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

# Docker Compose static variables (hardcoded in docker-compose.yml, not user-configurable)
grep -v -E '^(CHATBOT_HOST|CHATBOT_PORT|WEAVIATE_PORT)$' "$USED_VARS_FILE" > "${USED_VARS_FILE}.tmp" && mv "${USED_VARS_FILE}.tmp" "$USED_VARS_FILE"

TOTAL_USED=$(wc -l < "$USED_VARS_FILE")
echo -e "${GREEN}✓ Found ${TOTAL_USED} unique environment variables in code${NC}"
echo ""

echo -e "${YELLOW}Step 2: Extracting variables from .env.example...${NC}"

# Extract vars from .env.example
grep -E '^[A-Z_][A-Z0-9_]*=' .env.example 2>/dev/null | \
  cut -d'=' -f1 | \
  sort -u > "$ENV_EXAMPLE_VARS_FILE" || true

TOTAL_ENV_EXAMPLE=$(wc -l < "$ENV_EXAMPLE_VARS_FILE")
echo -e "${GREEN}✓ Found ${TOTAL_ENV_EXAMPLE} variables in .env.example${NC}"
echo ""

echo -e "${YELLOW}Step 3: Extracting variables from docker-compose.yml...${NC}"

# Extract from docker-compose.yml (variables referenced as ${VAR})
find . -maxdepth 1 -type f \( -name "*docker-compose.yml" -o -name "*docker-compose.yaml" \) -exec grep -oh \
  '\${[A-Z_][A-Z0-9_]*}' \
  {} + 2>/dev/null | \
  sed -E 's/\$\{([A-Z_][A-Z0-9_]*)\}/\1/' | \
  sort -u > "$DOCKER_COMPOSE_VARS_FILE" || true

TOTAL_DOCKER=$(wc -l < "$DOCKER_COMPOSE_VARS_FILE")
echo -e "${GREEN}✓ Found ${TOTAL_DOCKER} variables referenced in docker-compose files${NC}"
echo ""

# Check: Variables used in code but missing from .env.example
echo -e "${YELLOW}Checking: Variables in code but missing from .env.example...${NC}"
MISSING_IN_ENV_EXAMPLE=$(comm -23 "$USED_VARS_FILE" "$ENV_EXAMPLE_VARS_FILE")

if [ -n "$MISSING_IN_ENV_EXAMPLE" ]; then
  echo -e "${RED}✗ ERROR: The following variables are used in code but not in .env.example:${NC}"
  echo "$MISSING_IN_ENV_EXAMPLE" | sed 's/^/  - /'
  echo ""
  HAS_ERRORS=1
else
  echo -e "${GREEN}✓ All code variables are in .env.example${NC}"
  echo ""
fi

# Check: Variables in docker-compose not in .env.example
echo -e "${YELLOW}Checking: Variables in docker-compose but missing from .env.example...${NC}"
MISSING_DOCKER=$(comm -23 "$DOCKER_COMPOSE_VARS_FILE" "$ENV_EXAMPLE_VARS_FILE")

# Filter out CI-specific variables (set dynamically by GitHub Actions workflows, not user-configurable)
# These are set by workflow inputs and should NOT be in .env.example
MISSING_DOCKER=$(echo "$MISSING_DOCKER" | grep -v '^IMAGE_BACKEND$' || true)
MISSING_DOCKER=$(echo "$MISSING_DOCKER" | grep -v '^IMAGE_FRONTEND$' || true)
# Add more CI-only vars here if needed in the future

if [ -n "$MISSING_DOCKER" ]; then
  echo -e "${RED}✗ ERROR: The following variables are in docker-compose but not in .env.example:${NC}"
  echo "$MISSING_DOCKER" | sed 's/^/  - /'
  echo ""
  HAS_ERRORS=1
else
  echo -e "${GREEN}✓ All docker-compose variables are in .env.example${NC}"
  echo ""
fi

echo -e "${BLUE}================================================${NC}"
echo -e "${BLUE}  Summary${NC}"
echo -e "${BLUE}================================================${NC}"
echo ""

if [ $HAS_ERRORS -eq 0 ] && [ $HAS_WARNINGS -eq 0 ]; then
  echo -e "${GREEN}✓ All checks passed! Environment variables are properly configured.${NC}"
  exit 0
elif [ $HAS_ERRORS -eq 0 ]; then
  echo -e "${YELLOW}⚠ Validation passed with warnings.${NC}"
  exit 0
else
  echo -e "${RED}✗ Validation failed! Please fix the errors above.${NC}"
  exit 1
fi
