#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Kubernetes Deployment Script
# ─────────────────────────────────────────────────────────────────────────────
# Usage:
#   ./deploy/k8s-deploy.sh                     # interactive prompts (cloud)
#   ./deploy/k8s-deploy.sh --local             # local K8s (OrbStack, Docker Desktop)
#   ./deploy/k8s-deploy.sh --skip-build        # skip image build (images already pushed)
#   ./deploy/k8s-deploy.sh --skip-vault        # skip vault setup (already initialized)
#   ./deploy/k8s-deploy.sh --values-only       # only generate values file, don't deploy
#
# Required tools: kubectl, helm, docker (with buildx), yq, openssl
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/helm/aurora"
VALUES_FILE="$CHART_DIR/values.generated.yaml"
NAMESPACE="aurora-oss"
RELEASE="aurora-oss"

SKIP_BUILD=false
SKIP_VAULT=false
VALUES_ONLY=false
LOCAL_MODE=false

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
    --skip-vault) SKIP_VAULT=true ;;
    --values-only) VALUES_ONLY=true ;;
    --local) LOCAL_MODE=true ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

# ─── Helpers ─────────────────────────────────────────────────────────────────

prompt() {
  local var="$1" msg="$2" default="${3:-}"
  if [[ -n "$default" ]]; then
    read -rp "$msg [$default]: " val
    eval "$var=\"${val:-$default}\""
  else
    read -rp "$msg: " val
    while [[ -z "$val" ]]; do read -rp "$msg (required): " val; done
    eval "$var=\"$val\""
  fi
}

info() { echo -e "\033[1;34m→\033[0m $1"; }
ok()   { echo -e "\033[1;32m✓\033[0m $1"; }
warn() { echo -e "\033[1;33m!\033[0m $1"; }

# ─── Preflight ───────────────────────────────────────────────────────────────

info "Checking prerequisites..."
for cmd in kubectl helm docker yq openssl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing: $cmd"; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "Missing: docker buildx"; exit 1; }
ok "All tools found"

# ─── Gather inputs ───────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════"
if $LOCAL_MODE; then
  echo "  Aurora Local Kubernetes Deployment"
else
  echo "  Aurora Kubernetes Deployment"
fi
echo "═══════════════════════════════════════════════"
echo ""

if $LOCAL_MODE; then
  REGISTRY="localhost"
  IMAGE_TAG="local"
  info "Local mode — images will be built locally (no push, no cross-compile)"

  # Auto-detect ingress IP or prompt
  DETECTED_IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [[ -n "$DETECTED_IP" ]]; then
    info "Detected ingress IP: $DETECTED_IP"
    prompt INGRESS_IP "Ingress IP" "$DETECTED_IP"
  else
    warn "No ingress controller found. Install one first:"
    echo "  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml"
    prompt INGRESS_IP "Ingress controller IP (once installed)"
  fi
else
  prompt REGISTRY "Container registry (e.g. gcr.io/my-project, docker.io/myuser, ghcr.io/myorg)"
  IMAGE_TAG=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
  prompt INGRESS_IP "Ingress controller external IP (run: kubectl get svc -n ingress-nginx ingress-nginx-controller)"

  echo ""
  info "Storage — S3-compatible object storage"
  prompt STORAGE_BUCKET "Bucket name"
  prompt STORAGE_ENDPOINT "Endpoint URL" "https://s3.amazonaws.com"
  prompt STORAGE_REGION "Region" "us-east-1"
  prompt STORAGE_ACCESS_KEY "Access key"
  prompt STORAGE_SECRET_KEY "Secret key"
fi

echo ""
info "LLM provider"
prompt LLM_PROVIDER "Provider (openrouter, openai, anthropic, google)" "openrouter"
prompt LLM_API_KEY "API key for $LLM_PROVIDER"

echo ""
info "Environment"
if $LOCAL_MODE; then
  prompt AURORA_ENV "Environment (dev, staging, production)" "dev"
else
  prompt AURORA_ENV "Environment (dev, staging, production)" "staging"
fi

# ─── Generate values ─────────────────────────────────────────────────────────

POSTGRES_PW=$(openssl rand -base64 32)
FLASK_SECRET=$(openssl rand -base64 32)
AUTH_SECRET=$(openssl rand -base64 32)
SEARXNG_SECRET=$(openssl rand -base64 32)

info "Generating $VALUES_FILE ..."
cp "$CHART_DIR/values.yaml" "$VALUES_FILE"

# Image
yq -i ".image.registry = \"$REGISTRY\"" "$VALUES_FILE"
yq -i ".image.tag = \"$IMAGE_TAG\"" "$VALUES_FILE"

# URLs (nip.io)
yq -i ".config.AURORA_ENV = \"$AURORA_ENV\"" "$VALUES_FILE"
yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"http://api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"ws://ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
yq -i ".config.FRONTEND_URL = \"http://aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
yq -i ".config.SEARXNG_BASE_URL = \"http://aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"

if $LOCAL_MODE; then
  # MinIO — built-in S3-compatible storage
  yq -i '.services.minio.enabled = true' "$VALUES_FILE"
  yq -i '.config.STORAGE_BUCKET = "aurora-storage"' "$VALUES_FILE"
  yq -i '.config.STORAGE_ENDPOINT_URL = ""' "$VALUES_FILE"
  yq -i '.config.STORAGE_USE_SSL = "false"' "$VALUES_FILE"
  yq -i '.config.STORAGE_VERIFY_SSL = "false"' "$VALUES_FILE"
  yq -i '.secrets.backend.STORAGE_ACCESS_KEY = "minioadmin"' "$VALUES_FILE"
  yq -i '.secrets.backend.STORAGE_SECRET_KEY = "minioadmin"' "$VALUES_FILE"
else
  # External S3 storage
  yq -i ".config.STORAGE_BUCKET = \"$STORAGE_BUCKET\"" "$VALUES_FILE"
  yq -i ".config.STORAGE_ENDPOINT_URL = \"$STORAGE_ENDPOINT\"" "$VALUES_FILE"
  yq -i ".config.STORAGE_REGION = \"$STORAGE_REGION\"" "$VALUES_FILE"
  yq -i ".secrets.backend.STORAGE_ACCESS_KEY = \"$STORAGE_ACCESS_KEY\"" "$VALUES_FILE"
  yq -i ".secrets.backend.STORAGE_SECRET_KEY = \"$STORAGE_SECRET_KEY\"" "$VALUES_FILE"
fi

# Secrets
yq -i ".secrets.db.POSTGRES_PASSWORD = \"$POSTGRES_PW\"" "$VALUES_FILE"
yq -i ".secrets.app.FLASK_SECRET_KEY = \"$FLASK_SECRET\"" "$VALUES_FILE"
yq -i ".secrets.app.AUTH_SECRET = \"$AUTH_SECRET\"" "$VALUES_FILE"
yq -i ".secrets.app.SEARXNG_SECRET = \"$SEARXNG_SECRET\"" "$VALUES_FILE"

# LLM
LLM_KEY_FIELD="OPENROUTER_API_KEY"
case "$LLM_PROVIDER" in
  openai) LLM_KEY_FIELD="OPENAI_API_KEY" ;;
  anthropic) LLM_KEY_FIELD="ANTHROPIC_API_KEY" ;;
  google) LLM_KEY_FIELD="GOOGLE_AI_API_KEY" ;;
esac
yq -i ".config.LLM_PROVIDER_MODE = \"$LLM_PROVIDER\"" "$VALUES_FILE"
yq -i ".secrets.llm.${LLM_KEY_FIELD} = \"$LLM_API_KEY\"" "$VALUES_FILE"

# Ingress hosts
yq -i ".ingress.hosts.frontend = \"aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
yq -i ".ingress.hosts.api = \"api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
yq -i ".ingress.hosts.ws = \"ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"

ok "Values file generated: $VALUES_FILE"
ok "Image tag: $IMAGE_TAG"

if $VALUES_ONLY; then
  info "Values-only mode — stopping here."
  exit 0
fi

# ─── Build images ────────────────────────────────────────────────────────────

if $SKIP_BUILD; then
  warn "Skipping image build (--skip-build)"
elif $LOCAL_MODE; then
  info "Building aurora-server (localhost/aurora-server:local) ..."
  DOCKER_BUILDKIT=1 docker build "$REPO_ROOT/server" --target=prod -t localhost/aurora-server:local
  ok "Server image built"

  info "Building aurora-frontend (localhost/aurora-frontend:local) ..."
  DOCKER_BUILDKIT=1 docker build "$REPO_ROOT/client" --target=prod --build-arg "NEXT_PUBLIC_BACKEND_URL=http://api.aurora-oss.${INGRESS_IP}.nip.io" --build-arg "NEXT_PUBLIC_WEBSOCKET_URL=ws://ws.aurora-oss.${INGRESS_IP}.nip.io" -t localhost/aurora-frontend:local
  ok "Frontend image built"
else
  info "Building aurora-server ($REGISTRY/aurora-server:$IMAGE_TAG) ..."
  DOCKER_BUILDKIT=1 docker buildx build "$REPO_ROOT/server" --target=prod --platform linux/amd64 --push -t "$REGISTRY/aurora-server:$IMAGE_TAG"
  ok "Server image pushed"

  info "Building aurora-frontend ($REGISTRY/aurora-frontend:$IMAGE_TAG) ..."
  DOCKER_BUILDKIT=1 docker buildx build "$REPO_ROOT/client" --target=prod --platform linux/amd64 --push --build-arg "NEXT_PUBLIC_BACKEND_URL=http://api.aurora-oss.${INGRESS_IP}.nip.io" --build-arg "NEXT_PUBLIC_WEBSOCKET_URL=ws://ws.aurora-oss.${INGRESS_IP}.nip.io" -t "$REGISTRY/aurora-frontend:$IMAGE_TAG"
  ok "Frontend image pushed"
fi

# ─── Deploy with Helm ────────────────────────────────────────────────────────

info "Deploying with Helm..."
helm upgrade --install "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" --create-namespace --reset-values -f "$VALUES_FILE"
ok "Helm release deployed"

info "Waiting for pods..."
kubectl rollout status deployment -n "$NAMESPACE" --timeout=180s || true
kubectl get pods -n "$NAMESPACE"

# ─── Vault setup ─────────────────────────────────────────────────────────────

if $SKIP_VAULT; then
  warn "Skipping vault setup (--skip-vault)"
else
  info "Waiting for vault pod..."
  kubectl wait --for=condition=ready=false pod/aurora-oss-vault-0 -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
  sleep 5

  info "Initializing Vault..."
  VAULT_INIT=$(kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- vault operator init -key-shares=1 -key-threshold=1 2>&1)
  UNSEAL_KEY=$(echo "$VAULT_INIT" | grep "Unseal Key 1:" | awk '{print $NF}')
  ROOT_TOKEN=$(echo "$VAULT_INIT" | grep "Initial Root Token:" | awk '{print $NF}')
  ok "Vault initialized"
  echo "  Unseal Key:  $UNSEAL_KEY"
  echo "  Root Token:  $ROOT_TOKEN"
  warn "SAVE THESE — you need the unseal key after every vault restart"

  info "Unsealing Vault..."
  kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- vault operator unseal "$UNSEAL_KEY" >/dev/null 2>&1
  ok "Vault unsealed"

  info "Configuring Vault KV engine..."
  kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && echo \"$ROOT_TOKEN\" | vault login - >/dev/null 2>&1"
  kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2 >/dev/null 2>&1"
  kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- sh -c 'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - >/dev/null 2>&1 <<EOF
path "aurora/data/users/*" { capabilities = ["create","read","update","delete","list"] }
path "aurora/metadata/users/*" { capabilities = ["list","read","delete"] }
path "aurora/metadata/" { capabilities = ["list"] }
path "aurora/metadata/users" { capabilities = ["list"] }
EOF'

  APP_TOKEN=$(kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0 -format=json 2>/dev/null" | python3 -c "import sys,json; print(json.load(sys.stdin)['auth']['client_token'])")
  ok "Vault configured — app token created"

  info "Updating values with Vault token and redeploying..."
  yq -i ".secrets.backend.VAULT_TOKEN = \"$APP_TOKEN\"" "$VALUES_FILE"
  helm upgrade "$RELEASE" "$CHART_DIR" --reset-values -f "$VALUES_FILE" -n "$NAMESPACE"
  kubectl rollout status deployment -n "$NAMESPACE" --timeout=120s || true
  ok "Redeployed with Vault token"
fi

# ─── Verify ──────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════"
info "Deployment complete!"
echo "═══════════════════════════════════════════════"
echo ""
kubectl get pods -n "$NAMESPACE"
echo ""
echo "  Frontend:  http://aurora-oss.${INGRESS_IP}.nip.io"
echo "  API:       http://api.aurora-oss.${INGRESS_IP}.nip.io/health/"
echo "  WebSocket: ws://ws.aurora-oss.${INGRESS_IP}.nip.io"
echo ""
