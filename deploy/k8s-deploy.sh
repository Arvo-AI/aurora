#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Kubernetes Deployment Script
# ─────────────────────────────────────────────────────────────────────────────
# Usage:
#   ./deploy/k8s-deploy.sh                     # interactive prompts (cloud)
#   ./deploy/k8s-deploy.sh --local             # local K8s (OrbStack, Docker Desktop)
#   ./deploy/k8s-deploy.sh --private           # private/VPN deployment (internal LB, private hostname)
#   ./deploy/k8s-deploy.sh --skip-build        # skip image build (images already pushed)
#   ./deploy/k8s-deploy.sh --skip-vault        # skip vault setup (already initialized)
#   ./deploy/k8s-deploy.sh --values-only       # only generate values file, don't deploy
#
# Required tools: kubectl, helm, docker (with buildx), yq, openssl, python3
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
PRIVATE_MODE=false
INGRESS_IP=""
PRIVATE_HOSTNAME=""

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
    --skip-vault) SKIP_VAULT=true ;;
    --values-only) VALUES_ONLY=true ;;
    --local) LOCAL_MODE=true ;;
    --private) PRIVATE_MODE=true ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

# ─── Helpers ─────────────────────────────────────────────────────────────────

prompt() {
  local var="$1" msg="$2" default="${3:-}"
  if [[ -n "$default" ]]; then
    read -rp "$msg [$default]: " val
    printf -v "$var" '%s' "${val:-$default}"
  else
    read -rp "$msg: " val
    while [[ -z "$val" ]]; do read -rp "$msg (required): " val; done
    printf -v "$var" '%s' "$val"
  fi
}

info() { echo -e "\033[1;34m→\033[0m $1"; }
ok()   { echo -e "\033[1;32m✓\033[0m $1"; }
warn() { echo -e "\033[1;33m!\033[0m $1"; }

ensure_nginx_ingress() {
  if kubectl get ns ingress-nginx &>/dev/null && \
     kubectl get deployment -n ingress-nginx ingress-nginx-controller &>/dev/null; then
    ok "Nginx Ingress Controller already installed"
    return
  fi
  info "Installing Nginx Ingress Controller..."
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml
  info "Waiting for ingress controller to become ready..."
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s
  ok "Nginx Ingress Controller ready"
}

wait_for_ingress_ip() {
  local ns="$1" svc="ingress-nginx-controller"
  info "Waiting for ingress external IP (this can take 1-3 minutes for cloud load balancers)..." >&2
  local ip=""
  local attempts=0
  while [[ -z "$ip" && $attempts -lt 40 ]]; do
    ip=$(kubectl get svc "$svc" -n "$ns" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -z "$ip" ]]; then
      local host
      host=$(kubectl get svc "$svc" -n "$ns" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
      if [[ -n "$host" ]]; then
        ip=$(dig +short "$host" 2>/dev/null | head -1)
        [[ -z "$ip" ]] && ip=$(nslookup "$host" 2>/dev/null | awk '/^Address: / { print $2 }' | head -1)
      fi
    fi
    [[ -z "$ip" ]] && { sleep 5; (( attempts++ )) || true; }
  done
  echo "$ip"
}

# ─── Preflight ───────────────────────────────────────────────────────────────

info "Checking prerequisites..."
for cmd in kubectl helm docker yq openssl python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "Missing: $cmd"; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "Missing: docker buildx"; exit 1; }
ok "All tools found"

# ─── Gather inputs ───────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════"
if $LOCAL_MODE; then
  echo "  Aurora Local Kubernetes Deployment"
elif $PRIVATE_MODE; then
  echo "  Aurora Private/VPN Kubernetes Deployment"
else
  echo "  Aurora Kubernetes Deployment"
fi
echo "═══════════════════════════════════════════════"
echo ""

if $LOCAL_MODE; then
  REGISTRY="localhost"
  IMAGE_TAG="local"
  info "Local mode: images will be built locally (no push, no cross-compile)"

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
elif $PRIVATE_MODE; then
  if $SKIP_BUILD; then
    REGISTRY="ghcr.io/arvo-ai"
    IMAGE_TAG="latest"
    info "Using prebuilt images from $REGISTRY (tag: $IMAGE_TAG)"
  else
    prompt REGISTRY "Container registry (e.g. gcr.io/my-project, docker.io/myuser, ghcr.io/myorg)"
    IMAGE_TAG=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
  fi
  echo ""
  info "Private/VPN mode: the ingress load balancer will use an internal/private IP."
  info "Users must be on your VPN or within your VPC to reach Aurora."
  prompt PRIVATE_HOSTNAME "Private hostname for Aurora (e.g. aurora.internal, aurora.company.vpn)"
  warn "DNS for $PRIVATE_HOSTNAME must resolve on your VPN."
  warn "Options: split-horizon DNS, /etc/hosts on each client, or Tailscale MagicDNS."
  echo ""
  info "Storage: S3-compatible object storage"
  prompt STORAGE_BUCKET "Bucket name"
  prompt STORAGE_ENDPOINT "Endpoint URL" "https://s3.amazonaws.com"
  prompt STORAGE_REGION "Region" "us-east-1"
  prompt STORAGE_ACCESS_KEY "Access key"
  prompt STORAGE_SECRET_KEY "Secret key"
elif $SKIP_BUILD; then
  REGISTRY="ghcr.io/arvo-ai"
  IMAGE_TAG="latest"
  info "Using prebuilt images from $REGISTRY (tag: $IMAGE_TAG)"
  info "Ingress IP will be detected automatically after deploy."

  echo ""
  info "Storage: S3-compatible object storage"
  prompt STORAGE_BUCKET "Bucket name"
  prompt STORAGE_ENDPOINT "Endpoint URL" "https://s3.amazonaws.com"
  prompt STORAGE_REGION "Region" "us-east-1"
  prompt STORAGE_ACCESS_KEY "Access key"
  prompt STORAGE_SECRET_KEY "Secret key"
else
  prompt REGISTRY "Container registry (e.g. gcr.io/my-project, docker.io/myuser, ghcr.io/myorg)"
  IMAGE_TAG=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
  info "Ingress IP will be detected automatically after deploy."

  echo ""
  info "Storage: S3-compatible object storage"
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

# ─── Validate inputs ─────────────────────────────────────────────────────────

echo ""
info "Validating inputs..."
VALIDATION_FAILED=false

if [[ -n "${STORAGE_BUCKET:-}" && -n "${STORAGE_ACCESS_KEY:-}" && -n "${STORAGE_SECRET_KEY:-}" ]]; then
  ENDPOINT="${STORAGE_ENDPOINT:-https://s3.amazonaws.com}"
  REGION="${STORAGE_REGION:-us-east-1}"
  if command -v aws &>/dev/null; then
    if AWS_ACCESS_KEY_ID="$STORAGE_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$STORAGE_SECRET_KEY" \
       aws s3api head-bucket --bucket "$STORAGE_BUCKET" --region "$REGION" \
       --endpoint-url "$ENDPOINT" 2>/dev/null; then
      ok "S3 bucket '$STORAGE_BUCKET' is reachable"
    else
      warn "Could not reach S3 bucket '$STORAGE_BUCKET' — check bucket name, credentials, endpoint, and region"
      VALIDATION_FAILED=true
    fi
  else
    info "aws CLI not installed — skipping S3 bucket validation"
  fi
fi

case "$LLM_PROVIDER" in
  openrouter|openai|anthropic|google) ok "LLM provider '$LLM_PROVIDER' is valid" ;;
  *) warn "Unknown LLM provider '$LLM_PROVIDER' — expected: openrouter, openai, anthropic, or google"; VALIDATION_FAILED=true ;;
esac

if [[ -z "$LLM_API_KEY" || ${#LLM_API_KEY} -lt 10 ]]; then
  warn "LLM API key looks too short — double-check it"
  VALIDATION_FAILED=true
fi

if $VALIDATION_FAILED; then
  echo ""
  read -rp "Warnings found. Continue anyway? [y/N]: " cont
  [[ "$cont" =~ ^[Yy] ]] || { echo "Aborting."; exit 1; }
fi

echo ""
info "Environment"
if $LOCAL_MODE; then
  prompt AURORA_ENV "Environment (dev, staging, production)" "dev"
else
  prompt AURORA_ENV "Environment (dev, staging, production)" "staging"
fi

# ─── Ensure nginx ingress + detect IP (cloud / private) ─────────────────────

if ! $LOCAL_MODE && ! $VALUES_ONLY; then
  ensure_nginx_ingress
  INGRESS_IP=$(wait_for_ingress_ip ingress-nginx)
  if [[ -z "$INGRESS_IP" ]]; then
    warn "Could not detect ingress IP automatically."
    prompt INGRESS_IP "Enter ingress IP or hostname manually"
  else
    ok "Ingress IP: $INGRESS_IP"
  fi
fi

# ─── Generate values ─────────────────────────────────────────────────────────

POSTGRES_PW=$(openssl rand -base64 32)
FLASK_SECRET=$(openssl rand -base64 32)
AUTH_SECRET=$(openssl rand -base64 32)
INTERNAL_API_SECRET=$(openssl rand -base64 32)
SEARXNG_SECRET=$(openssl rand -base64 32)
MEMGRAPH_PW=$(openssl rand -base64 32)

info "Generating $VALUES_FILE ..."
cp "$CHART_DIR/values.yaml" "$VALUES_FILE"

# Image
yq -i ".image.registry = \"$REGISTRY\"" "$VALUES_FILE"
yq -i ".image.tag = \"$IMAGE_TAG\"" "$VALUES_FILE"

# URLs (nip.io for local/cloud, private hostname for --private)
yq -i ".config.AURORA_ENV = \"$AURORA_ENV\"" "$VALUES_FILE"
if $PRIVATE_MODE; then
  yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"http://api.${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"ws://ws.${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".config.FRONTEND_URL = \"http://${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.frontend = \"${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.api = \"api.${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.ws = \"ws.${PRIVATE_HOSTNAME}\"" "$VALUES_FILE"
  yq -i ".ingress.internal = true" "$VALUES_FILE"

  # Auto-detect cloud provider from kubectl context and apply internal LB annotation
  KUBE_CONTEXT=$(kubectl config current-context 2>/dev/null || true)
  if [[ "$KUBE_CONTEXT" == gke_* ]]; then
    yq -i '.ingress.annotations["cloud.google.com/load-balancer-type"] = "Internal"' "$VALUES_FILE"
    ok "Detected GKE — applied internal load balancer annotation"
  elif [[ "$KUBE_CONTEXT" == *arn:aws* ]] || [[ "$KUBE_CONTEXT" == *eks* ]]; then
    yq -i '.ingress.annotations["service.beta.kubernetes.io/aws-load-balancer-internal"] = "true"' "$VALUES_FILE"
    ok "Detected EKS — applied internal load balancer annotation"
  elif [[ "$KUBE_CONTEXT" == *aks* ]]; then
    yq -i '.ingress.annotations["service.beta.kubernetes.io/azure-load-balancer-internal"] = "true"' "$VALUES_FILE"
    ok "Detected AKS — applied internal load balancer annotation"
  else
    warn "Could not detect cloud provider from context '$KUBE_CONTEXT'."
    warn "To get a VPC-internal load balancer, manually add the annotation for your provider:"
    warn "  GKE: ingress.annotations[cloud.google.com/load-balancer-type] = Internal"
    warn "  EKS: ingress.annotations[service.beta.kubernetes.io/aws-load-balancer-internal] = true"
    warn "  AKS: ingress.annotations[service.beta.kubernetes.io/azure-load-balancer-internal] = true"
  fi
else
  yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"http://api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
  yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"ws://ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
  yq -i ".config.FRONTEND_URL = \"http://aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.frontend = \"aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.api = \"api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
  yq -i ".ingress.hosts.ws = \"ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES_FILE"
fi

if $LOCAL_MODE; then
  # MinIO: built-in S3-compatible storage
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
yq -i ".secrets.app.INTERNAL_API_SECRET = \"$INTERNAL_API_SECRET\"" "$VALUES_FILE"
yq -i ".secrets.app.SEARXNG_SECRET = \"$SEARXNG_SECRET\"" "$VALUES_FILE"
yq -i ".secrets.app.MEMGRAPH_PASSWORD = \"$MEMGRAPH_PW\"" "$VALUES_FILE"

# LLM
LLM_KEY_FIELD="OPENROUTER_API_KEY"
case "$LLM_PROVIDER" in
  openai) LLM_KEY_FIELD="OPENAI_API_KEY" ;;
  anthropic) LLM_KEY_FIELD="ANTHROPIC_API_KEY" ;;
  google) LLM_KEY_FIELD="GOOGLE_AI_API_KEY" ;;
esac
yq -i ".config.LLM_PROVIDER_MODE = \"$(if [[ "$LLM_PROVIDER" == "openrouter" ]]; then echo openrouter; else echo direct; fi)\"" "$VALUES_FILE"
yq -i ".secrets.llm.${LLM_KEY_FIELD} = \"$LLM_API_KEY\"" "$VALUES_FILE"

ok "Values file generated: $VALUES_FILE"
ok "Image tag: $IMAGE_TAG"

if $VALUES_ONLY; then
  info "Values-only mode: stopping here."
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
  # Verify registry credentials before building
  info "Verifying registry credentials..."
  if ! docker login "$REGISTRY" --get-login >/dev/null 2>&1; then
    warn "Not logged in to $REGISTRY. Attempting login..."
    docker login "$REGISTRY" || { echo "Registry login failed. Run 'docker login $REGISTRY' first."; exit 1; }
  fi
  ok "Registry credentials valid"

  info "Building aurora-server ($REGISTRY/aurora-server:$IMAGE_TAG) ..."
  DOCKER_BUILDKIT=1 docker buildx build "$REPO_ROOT/server" --target=prod --platform linux/amd64 --push -t "$REGISTRY/aurora-server:$IMAGE_TAG"
  ok "Server image pushed"

  if $PRIVATE_MODE; then
    FRONTEND_BACKEND_URL="http://api.${PRIVATE_HOSTNAME}"
    FRONTEND_WS_URL="ws://ws.${PRIVATE_HOSTNAME}"
  else
    FRONTEND_BACKEND_URL="http://api.aurora-oss.${INGRESS_IP}.nip.io"
    FRONTEND_WS_URL="ws://ws.aurora-oss.${INGRESS_IP}.nip.io"
  fi

  info "Building aurora-frontend ($REGISTRY/aurora-frontend:$IMAGE_TAG) ..."
  DOCKER_BUILDKIT=1 docker buildx build "$REPO_ROOT/client" --target=prod --platform linux/amd64 --push --build-arg "NEXT_PUBLIC_BACKEND_URL=${FRONTEND_BACKEND_URL}" --build-arg "NEXT_PUBLIC_WEBSOCKET_URL=${FRONTEND_WS_URL}" -t "$REGISTRY/aurora-frontend:$IMAGE_TAG"
  ok "Frontend image pushed"
fi

# ─── Deploy with Helm ────────────────────────────────────────────────────────

info "Deploying with Helm..."
helm upgrade --install "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" --create-namespace --reset-values -f "$VALUES_FILE"
ok "Helm release deployed"

info "Waiting for pods..."
if ! kubectl rollout status deployment -n "$NAMESPACE" --timeout=180s; then
  warn "Some deployments did not become ready within 180s. Check pod status below."
fi
kubectl get pods -n "$NAMESPACE"

# ─── Vault setup ─────────────────────────────────────────────────────────────

if $SKIP_VAULT; then
  warn "Skipping vault setup (--skip-vault)"
else
  info "Waiting for vault pod..."
  until kubectl get pod/aurora-oss-vault-0 -n "$NAMESPACE" &>/dev/null; do
    echo "Waiting for Vault pod to be created..."
    sleep 2
  done
  kubectl wait --for=condition=ContainersReady=false pod/aurora-oss-vault-0 -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
  sleep 3

  info "Initializing Vault..."
  VAULT_INIT=$(kubectl -n "$NAMESPACE" exec statefulset/aurora-oss-vault -- vault operator init -key-shares=1 -key-threshold=1 2>&1)
  UNSEAL_KEY=$(echo "$VAULT_INIT" | grep "Unseal Key 1:" | awk '{print $NF}')
  ROOT_TOKEN=$(echo "$VAULT_INIT" | grep "Initial Root Token:" | awk '{print $NF}')
  ok "Vault initialized"

  CREDENTIALS_FILE="$REPO_ROOT/vault-init-${RELEASE}.txt"
  umask 077
  cat > "$CREDENTIALS_FILE" <<CEOF
Unseal Key: $UNSEAL_KEY
Root Token: $ROOT_TOKEN
CEOF
  umask 022
  echo ""
  warn "╔══════════════════════════════════════════════════════════════╗"
  warn "║  Vault credentials written to: vault-init-${RELEASE}.txt   ║"
  warn "║  This file contains your Vault root token and unseal key.  ║"
  warn "║  → Move it to a password manager or secure vault           ║"
  warn "║  → Then delete it: rm $CREDENTIALS_FILE                    ║"
  warn "╚══════════════════════════════════════════════════════════════╝"
  echo ""

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
  ok "Vault configured: app token created"

  info "Updating values with Vault token and redeploying..."
  yq -i ".secrets.backend.VAULT_TOKEN = \"$APP_TOKEN\"" "$VALUES_FILE"
  helm upgrade "$RELEASE" "$CHART_DIR" --reset-values -f "$VALUES_FILE" -n "$NAMESPACE"
  if ! kubectl rollout status deployment -n "$NAMESPACE" --timeout=120s; then
    warn "Some deployments did not become ready after Vault redeploy. Check pod status."
  fi
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
if $PRIVATE_MODE; then
  echo "  Frontend:  http://${PRIVATE_HOSTNAME}"
  echo "  API:       http://api.${PRIVATE_HOSTNAME}/health/"
  echo "  WebSocket: ws://ws.${PRIVATE_HOSTNAME}"
  echo ""
  warn "Access requires VPN connectivity and DNS resolution for ${PRIVATE_HOSTNAME}."

  # Detect ingress IP for /etc/hosts guidance
  PRIVATE_INGRESS_IP=$(wait_for_ingress_ip ingress-nginx)
  if [[ -n "$PRIVATE_INGRESS_IP" ]]; then
    echo ""
    info "Ingress load balancer IP: $PRIVATE_INGRESS_IP"
    echo ""
    echo "  Add to /etc/hosts on each VPN client (or configure split-horizon DNS):"
    echo ""
    echo "    ${PRIVATE_INGRESS_IP}  ${PRIVATE_HOSTNAME} api.${PRIVATE_HOSTNAME} ws.${PRIVATE_HOSTNAME}"
    echo ""
    read -rp "Add this entry to your local /etc/hosts now? [y/N] " ADD_HOSTS
    if [[ "${ADD_HOSTS,,}" == "y" ]]; then
      echo "${PRIVATE_INGRESS_IP}  ${PRIVATE_HOSTNAME} api.${PRIVATE_HOSTNAME} ws.${PRIVATE_HOSTNAME}" | sudo tee -a /etc/hosts >/dev/null
      ok "Added to /etc/hosts"
    fi
  else
    warn "Could not detect ingress IP yet. Once the load balancer is assigned, add to /etc/hosts:"
    echo "    <ingress-ip>  ${PRIVATE_HOSTNAME} api.${PRIVATE_HOSTNAME} ws.${PRIVATE_HOSTNAME}"
    echo "  Check IP with: kubectl get svc ingress-nginx-controller -n ingress-nginx"
  fi
else
  echo "  Frontend:  http://aurora-oss.${INGRESS_IP}.nip.io"
  echo "  API:       http://api.aurora-oss.${INGRESS_IP}.nip.io/health/"
  echo "  WebSocket: ws://ws.aurora-oss.${INGRESS_IP}.nip.io"
  echo ""
  warn "These nip.io URLs use a resolved IP that can change (especially on AWS ELB)."
  warn "For production, set up real DNS CNAME records + TLS with cert-manager."
  warn "See: website/docs/deployment/kubernetes.md → DNS & TLS"
fi
echo ""
