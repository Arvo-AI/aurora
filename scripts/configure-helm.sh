#!/usr/bin/env bash
set -euo pipefail

# Interactive Helm values configuration for Aurora Kubernetes deployments.
# Prompts for required values, generates secrets, and writes values.generated.yaml.
#
# Usage:
#   ./scripts/configure-helm.sh                    # interactive prompts
#   ./scripts/configure-helm.sh --non-interactive   # skip prompts, generate secrets only

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/helm/aurora"
VALUES_FILE="$CHART_DIR/values.generated.yaml"

NON_INTERACTIVE=false
if [ "${1:-}" = "--non-interactive" ]; then
  NON_INTERACTIVE=true
fi

generate_secret() {
  if command -v openssl &>/dev/null; then
    openssl rand -base64 32
  elif command -v python3 &>/dev/null; then
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  else
    cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 44 | head -n 1
  fi
}

if ! command -v yq &>/dev/null; then
  echo "Error: yq is required but not installed."
  echo "Install: https://github.com/mikefarah/yq#install"
  exit 1
fi

if [ ! -f "$VALUES_FILE" ]; then
  cp "$CHART_DIR/values.yaml" "$VALUES_FILE"
  echo "Created values.generated.yaml from values.yaml"
fi

echo "============================================"
echo "  Aurora Helm Configuration"
echo "============================================"
echo ""

# --- Generate secrets (always, idempotent) ---
echo "Generating secrets..."
POSTGRES_PW=$(yq '.secrets.db.POSTGRES_PASSWORD' "$VALUES_FILE")
if [ -z "$POSTGRES_PW" ] || [ "$POSTGRES_PW" = "null" ]; then
  yq -i ".secrets.db.POSTGRES_PASSWORD = \"$(generate_secret)\"" "$VALUES_FILE"
  echo "  Generated POSTGRES_PASSWORD"
else
  echo "  POSTGRES_PASSWORD already set, skipping"
fi

for key in FLASK_SECRET_KEY AUTH_SECRET SEARXNG_SECRET; do
  CURRENT=$(yq ".secrets.app.${key}" "$VALUES_FILE")
  if [ -z "$CURRENT" ] || [ "$CURRENT" = "null" ]; then
    yq -i ".secrets.app.${key} = \"$(generate_secret)\"" "$VALUES_FILE"
    echo "  Generated ${key}"
  else
    echo "  ${key} already set, skipping"
  fi
done
echo ""

if [ "$NON_INTERACTIVE" = true ]; then
  echo "Non-interactive mode — skipping prompts."
  echo "Edit ${VALUES_FILE} to configure LLM keys, URLs, and other settings."
  exit 0
fi

# --- LLM Provider ---
echo "LLM Provider Configuration"
echo "  1) OpenRouter  (recommended — one key, many models)"
echo "  2) OpenAI"
echo "  3) Anthropic"
echo "  4) Google AI"
echo "  5) Ollama      (local models, no internet needed)"
echo "  6) Skip        (configure later)"
printf "Choose [1]: "
read -r LLM_CHOICE
LLM_CHOICE="${LLM_CHOICE:-1}"

case "$LLM_CHOICE" in
  1)
    yq -i '.config.LLM_PROVIDER_MODE = "openrouter"' "$VALUES_FILE"
    printf "OpenRouter API key: "
    read -r API_KEY
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.OPENROUTER_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  2)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    printf "OpenAI API key: "
    read -r API_KEY
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.OPENAI_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  3)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    printf "Anthropic API key: "
    read -r API_KEY
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.ANTHROPIC_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  4)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    printf "Google AI API key: "
    read -r API_KEY
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.GOOGLE_AI_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  5)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    printf "Ollama URL [http://ollama-service:11434]: "
    read -r OLLAMA_URL
    OLLAMA_URL="${OLLAMA_URL:-http://ollama-service:11434}"
    yq -i ".config.OLLAMA_BASE_URL = \"${OLLAMA_URL}\"" "$VALUES_FILE"
    yq -i '.config.MAIN_MODEL = "ollama/llama3.1"' "$VALUES_FILE"
    yq -i '.config.RCA_MODEL = "ollama/llama3.1"' "$VALUES_FILE"
    echo "  Set Ollama as LLM provider with default models (ollama/llama3.1)"
    ;;
  6)
    echo "  Skipping LLM configuration. Set it later in ${VALUES_FILE}"
    ;;
esac
echo ""

# --- Domain & Ingress ---
echo "Domain Configuration"
echo "  Enter your base domain. Ingress hosts and public URLs will be derived from it."
echo "  Example: aurora.example.com"
echo ""

# Detect available ingress classes
AVAILABLE_CLASSES=$(kubectl get ingressclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
if [ -n "$AVAILABLE_CLASSES" ]; then
  CURRENT_CLASS=$(yq '.ingress.className // "nginx"' "$VALUES_FILE" 2>/dev/null || echo "nginx")
  echo "  Detected ingress classes: ${AVAILABLE_CLASSES}"
  if ! echo "$AVAILABLE_CLASSES" | grep -qw "$CURRENT_CLASS"; then
    FIRST_CLASS=$(echo "$AVAILABLE_CLASSES" | awk '{print $1}')
    printf "  Ingress class [${FIRST_CLASS}]: "
    read -r INGRESS_CLASS
    INGRESS_CLASS="${INGRESS_CLASS:-$FIRST_CLASS}"
  else
    printf "  Ingress class [${CURRENT_CLASS}]: "
    read -r INGRESS_CLASS
    INGRESS_CLASS="${INGRESS_CLASS:-$CURRENT_CLASS}"
  fi
  yq -i ".ingress.className = \"${INGRESS_CLASS}\"" "$VALUES_FILE"
  echo "  Set ingress.className = ${INGRESS_CLASS}"
  echo ""
fi

printf "Base domain (e.g. aurora.example.com): "
read -r BASE_DOMAIN
if [ -n "$BASE_DOMAIN" ]; then
  # Strip protocol if provided
  BASE_DOMAIN=$(echo "$BASE_DOMAIN" | sed 's|^https\?://||' | sed 's|/$||')

  FRONTEND_HOST="$BASE_DOMAIN"
  API_HOST="api.${BASE_DOMAIN}"
  WS_HOST="ws.${BASE_DOMAIN}"

  # Ingress hosts
  yq -i ".ingress.hosts.frontend = \"${FRONTEND_HOST}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.api = \"${API_HOST}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.ws = \"${WS_HOST}\"" "$VALUES_FILE"
  echo "  Ingress hosts:"
  echo "    frontend: ${FRONTEND_HOST}"
  echo "    api:      ${API_HOST}"
  echo "    ws:       ${WS_HOST}"

  # Public URLs (derived from ingress hosts)
  SCHEME="https"
  yq -i ".config.FRONTEND_URL = \"${SCHEME}://${FRONTEND_HOST}\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"${SCHEME}://${API_HOST}\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"wss://${WS_HOST}\"" "$VALUES_FILE"
  yq -i ".config.SEARXNG_BASE_URL = \"${SCHEME}://${FRONTEND_HOST}\"" "$VALUES_FILE"
  echo "  Public URLs set automatically from domain"

  # TLS
  echo ""
  printf "Enable TLS with cert-manager (auto Let's Encrypt)? [Y/n]: "
  read -r TLS_CHOICE
  TLS_CHOICE="${TLS_CHOICE:-Y}"
  if [ "$TLS_CHOICE" = "n" ] || [ "$TLS_CHOICE" = "N" ]; then
    yq -i '.ingress.tls.enabled = false' "$VALUES_FILE"
    echo "  TLS disabled"
  else
    yq -i '.ingress.tls.enabled = true' "$VALUES_FILE"
    yq -i '.ingress.tls.certManager.enabled = true' "$VALUES_FILE"
    printf "  cert-manager email (for Let's Encrypt): "
    read -r CERT_EMAIL
    if [ -n "$CERT_EMAIL" ]; then
      yq -i ".ingress.tls.certManager.email = \"${CERT_EMAIL}\"" "$VALUES_FILE"
    fi
    echo "  TLS enabled with cert-manager"
  fi
else
  echo "  Skipping domain configuration. Set it later in ${VALUES_FILE}"
fi
echo ""

echo "============================================"
echo "  Configuration saved to:"
echo "  ${VALUES_FILE}"
echo "============================================"
echo ""
echo "You can edit this file at any time to change settings:"
echo "  nano ${VALUES_FILE}"
echo ""
