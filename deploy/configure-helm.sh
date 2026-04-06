#!/usr/bin/env bash
set -euo pipefail

# Interactive Helm values configuration for Aurora Kubernetes deployments.
# Prompts for required values, generates secrets, and writes values.generated.yaml.
#
# Usage:
#   ./deploy/configure-helm.sh                    # interactive prompts
#   ./deploy/configure-helm.sh --non-interactive   # skip prompts, generate secrets only

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/helm/aurora"
VALUES_FILE="$CHART_DIR/values.generated.yaml"

source "$SCRIPT_DIR/lib/common.sh"

NON_INTERACTIVE=false
if [ "${1:-}" = "--non-interactive" ]; then
  NON_INTERACTIVE=true
fi

if ! check_tool yq; then
  warn "yq is required but not installed."
  echo "  Install: https://github.com/mikefarah/yq#install"
  exit 1
fi

if [ ! -f "$VALUES_FILE" ]; then
  cp "$CHART_DIR/values.yaml" "$VALUES_FILE"
  ok "Created values.generated.yaml from values.yaml"
fi

echo "============================================"
echo "  Aurora Helm Configuration"
echo "============================================"
echo ""

# --- Generate secrets (always, idempotent) ---
info "Generating secrets..."
POSTGRES_PW=$(yq '.secrets.db.POSTGRES_PASSWORD' "$VALUES_FILE")
if [ -z "$POSTGRES_PW" ] || [ "$POSTGRES_PW" = "null" ]; then
  yq -i ".secrets.db.POSTGRES_PASSWORD = \"$(generate_secret)\"" "$VALUES_FILE"
  echo "  Generated POSTGRES_PASSWORD"
else
  echo "  POSTGRES_PASSWORD already set, skipping"
fi

MEMGRAPH_PW=$(yq '.secrets.db.MEMGRAPH_PASSWORD' "$VALUES_FILE")
if [ -z "$MEMGRAPH_PW" ] || [ "$MEMGRAPH_PW" = "null" ]; then
  yq -i ".secrets.db.MEMGRAPH_PASSWORD = \"$(generate_secret)\"" "$VALUES_FILE"
  echo "  Generated MEMGRAPH_PASSWORD"
else
  echo "  MEMGRAPH_PASSWORD already set, skipping"
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
select_menu "LLM Provider Configuration" \
  "OpenRouter  (recommended — one key, many models)" \
  "OpenAI" \
  "Anthropic" \
  "Google AI" \
  "Ollama      (local models, no internet needed)" \
  "Skip        (configure later)"

case "$MENU_RESULT" in
  0)
    yq -i '.config.LLM_PROVIDER_MODE = "openrouter"' "$VALUES_FILE"
    prompt API_KEY "OpenRouter API key"
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.OPENROUTER_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  1)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    prompt API_KEY "OpenAI API key"
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.OPENAI_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  2)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    prompt API_KEY "Anthropic API key"
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.ANTHROPIC_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  3)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    prompt API_KEY "Google AI API key"
    if [ -n "$API_KEY" ]; then
      yq -i ".secrets.llm.GOOGLE_AI_API_KEY = \"${API_KEY}\"" "$VALUES_FILE"
    fi
    ;;
  4)
    yq -i '.config.LLM_PROVIDER_MODE = "direct"' "$VALUES_FILE"
    prompt OLLAMA_URL "Ollama URL" "http://ollama-service:11434"
    yq -i ".config.OLLAMA_BASE_URL = \"${OLLAMA_URL}\"" "$VALUES_FILE"
    yq -i '.config.MAIN_MODEL = "ollama/llama3.1"' "$VALUES_FILE"
    yq -i '.config.RCA_MODEL = "ollama/llama3.1"' "$VALUES_FILE"
    ok "Set Ollama as LLM provider with default models (ollama/llama3.1)"
    ;;
  5)
    info "Skipping LLM configuration. Set it later in ${VALUES_FILE}"
    ;;
esac
echo ""

# --- Domain & Ingress ---
info "Domain Configuration"
echo "  Enter your base domain. Ingress hosts and public URLs will be derived from it."
echo "  Example: aurora.example.com"
echo ""

AVAILABLE_CLASSES=$(kubectl get ingressclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
if [ -n "$AVAILABLE_CLASSES" ]; then
  CURRENT_CLASS=$(yq '.ingress.className // "nginx"' "$VALUES_FILE" 2>/dev/null || echo "nginx")
  info "Detected ingress classes: ${AVAILABLE_CLASSES}"
  if ! echo "$AVAILABLE_CLASSES" | grep -qw "$CURRENT_CLASS"; then
    FIRST_CLASS=$(echo "$AVAILABLE_CLASSES" | awk '{print $1}')
    prompt INGRESS_CLASS "Ingress class" "$FIRST_CLASS"
  else
    prompt INGRESS_CLASS "Ingress class" "$CURRENT_CLASS"
  fi
  yq -i ".ingress.className = \"${INGRESS_CLASS}\"" "$VALUES_FILE"
  ok "Set ingress.className = ${INGRESS_CLASS}"
  echo ""
fi

prompt BASE_DOMAIN "Base domain (e.g. aurora.example.com)" ""
if [ -n "$BASE_DOMAIN" ]; then
  BASE_DOMAIN=$(echo "$BASE_DOMAIN" | sed 's|^https\?://||' | sed 's|/$||')

  FRONTEND_HOST="$BASE_DOMAIN"
  API_HOST="api.${BASE_DOMAIN}"
  WS_HOST="ws.${BASE_DOMAIN}"

  yq -i ".ingress.hosts.frontend = \"${FRONTEND_HOST}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.api = \"${API_HOST}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.ws = \"${WS_HOST}\"" "$VALUES_FILE"
  echo "  Ingress hosts:"
  echo "    frontend: ${FRONTEND_HOST}"
  echo "    api:      ${API_HOST}"
  echo "    ws:       ${WS_HOST}"

  # TLS
  echo ""
  if confirm "Enable TLS with cert-manager? (requires cert-manager in cluster)"; then
    yq -i '.ingress.tls.enabled = true' "$VALUES_FILE"
    yq -i '.ingress.tls.certManager.enabled = true' "$VALUES_FILE"
    prompt CERT_EMAIL "cert-manager email (for Let's Encrypt)" ""
    if [ -n "$CERT_EMAIL" ]; then
      yq -i ".ingress.tls.certManager.email = \"${CERT_EMAIL}\"" "$VALUES_FILE"
    fi
    ok "TLS enabled with cert-manager"
    SCHEME="https"
    WS_SCHEME="wss"
  else
    yq -i '.ingress.tls.enabled = false' "$VALUES_FILE"
    info "TLS disabled"
    SCHEME="http"
    WS_SCHEME="ws"
  fi

  yq -i ".config.FRONTEND_URL = \"${SCHEME}://${FRONTEND_HOST}\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"${SCHEME}://${API_HOST}\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"${WS_SCHEME}://${WS_HOST}\"" "$VALUES_FILE"
  yq -i ".config.SEARXNG_BASE_URL = \"${SCHEME}://${FRONTEND_HOST}\"" "$VALUES_FILE"
  ok "Public URLs updated for ${SCHEME}://"
else
  info "Skipping domain configuration. Set it later in ${VALUES_FILE}"
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
