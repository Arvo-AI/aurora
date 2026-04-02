#!/usr/bin/env bash
set -euo pipefail

# Single-command Aurora deployment to Kubernetes with a private registry.
# Guides the user through every step: download → extract → push → configure → deploy.
#
# Idempotent: re-run safely after interruptions — completed steps are skipped.
#
# Usage:
#   ./scripts/deploy-k8s.sh <registry-url>
#   ./scripts/deploy-k8s.sh <registry-url> <version>
#
# Example:
#   ./scripts/deploy-k8s.sh registry.internal:5000
#   ./scripts/deploy-k8s.sh registry.internal:5000 v1.2.3
#
# Can also be run via curl on a fresh machine:
#   curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/deploy-k8s.sh | bash -s -- <registry-url>

REGISTRY="${1:-}"
VERSION="${2:-latest}"

if [ -z "$REGISTRY" ]; then
  echo "Usage: $0 <registry-url> [version]"
  echo ""
  echo "  registry-url  Target registry (e.g. registry.internal:5000)"
  echo "  version       Aurora version (default: latest release)"
  exit 1
fi

REGISTRY="${REGISTRY%/}"

echo "============================================"
echo "  Aurora Kubernetes Deployment"
echo "  Registry: $REGISTRY"
echo "  Version:  $VERSION"
echo "============================================"
echo ""

# ---- Pre-flight checks ----
MISSING=""
for cmd in kubectl helm yq curl tar openssl python3; do
  if ! command -v "$cmd" &>/dev/null; then
    MISSING="$MISSING $cmd"
  fi
done

if ! command -v skopeo &>/dev/null && ! command -v docker &>/dev/null; then
  MISSING="$MISSING skopeo-or-docker"
fi

if [ -n "$MISSING" ]; then
  echo "Missing required tools:$MISSING"
  exit 1
fi

# ---- Helpers ----
info() { echo -e "\033[1;34m→\033[0m $1"; }
ok()   { echo -e "\033[1;32m✓\033[0m $1"; }
warn() { echo -e "\033[1;33m!\033[0m $1"; }

# ---- Step 1: Download ----
echo "Resolving latest version from GitHub..."
LATEST_VERSION=$(curl -fsSL "https://api.github.com/repos/arvo-ai/aurora/releases/latest" 2>/dev/null | grep '"tag_name"' | cut -d'"' -f4 || true)
TARGET_VERSION="$VERSION"
if [ "$TARGET_VERSION" = "latest" ] && [ -n "$LATEST_VERSION" ]; then
  TARGET_VERSION="$LATEST_VERSION"
fi

TARBALL=""
for f in aurora-airtight-*.tar.gz; do
  [ -f "$f" ] || continue
  if echo "$f" | grep -q "$TARGET_VERSION"; then
    TARBALL="$f"
    break
  fi
done

SOURCE_ARCHIVE=""
if [ -n "$TARBALL" ]; then
  SOURCE_ARCHIVE=$(ls aurora-*.tar.gz 2>/dev/null | grep -v airtight | head -1 || true)
fi

# If running from inside the repo, we don't need the source archive
IN_REPO=false
if [ -d "deploy/helm/aurora" ] && [ -d "scripts" ]; then
  IN_REPO=true
fi

if [ -n "$TARBALL" ] && { [ -n "$SOURCE_ARCHIVE" ] || [ "$IN_REPO" = true ]; }; then
  FRESH_DOWNLOAD=false
  echo "=== Step 1/5: Download bundle — SKIPPED (found $TARBALL, matches $TARGET_VERSION) ==="
else
  FRESH_DOWNLOAD=true
  echo "=== Step 1/5: Download bundle (${TARGET_VERSION}) ==="
  echo ""

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd 2>/dev/null || pwd)"

  if [ -f "$SCRIPT_DIR/download-bundle.sh" ]; then
    bash "$SCRIPT_DIR/download-bundle.sh" "$TARGET_VERSION"
  else
    curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash -s -- "$TARGET_VERSION"
  fi

  TARBALL=$(ls aurora-airtight-*.tar.gz 2>/dev/null | head -1 || true)
  SOURCE_ARCHIVE=$(ls aurora-*.tar.gz 2>/dev/null | grep -v airtight | head -1 || true)
fi
echo ""

# ---- Step 2: Extract source archive ----
if [ "$IN_REPO" = true ]; then
  REPO_DIR="."
  echo "=== Step 2/5: Extract source archive — SKIPPED (already in repo) ==="
else
  REPO_DIR=$(find . -maxdepth 1 -type d -name "aurora-*" ! -name "aurora-airtight-*" | head -1 || true)

  if [ "$FRESH_DOWNLOAD" = false ] && [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR/deploy/helm" ]; then
    echo "=== Step 2/5: Extract source archive — SKIPPED (found $REPO_DIR) ==="
  else
    echo "=== Step 2/5: Extract source archive ==="
    echo ""

    if [ -z "$SOURCE_ARCHIVE" ]; then
      echo "Error: source archive not found. Expected aurora-<version>.tar.gz"
      exit 1
    fi

    tar xzf "$SOURCE_ARCHIVE"
    REPO_DIR=$(tar tzf "$SOURCE_ARCHIVE" | head -1 | cut -d/ -f1)
    echo "Extracted to $REPO_DIR"
  fi
fi
echo ""

cd "$REPO_DIR"

# ---- Step 3: Push images to registry ----
VALUES_FILE="deploy/helm/aurora/values.generated.yaml"
CURRENT_REGISTRY=""
if [ -f "$VALUES_FILE" ]; then
  CURRENT_REGISTRY=$(yq '.image.registry // ""' "$VALUES_FILE" 2>/dev/null || true)
fi

if [ "$FRESH_DOWNLOAD" = false ] && [ "$CURRENT_REGISTRY" = "$REGISTRY" ]; then
  echo "=== Step 3/5: Push images to registry — SKIPPED (already pushed to $REGISTRY) ==="
else
  echo "=== Step 3/5: Push images to registry ==="
  echo ""
  bash ./scripts/push-to-registry.sh "$REGISTRY"
fi
echo ""

# ---- Step 4: Configure ----
CONFIG_COMPLETE=true
CONFIG_MISSING=""
if [ -f "$VALUES_FILE" ]; then
  for path in \
    '.secrets.postgres.POSTGRES_PASSWORD' \
    '.secrets.app.FLASK_SECRET_KEY' \
    '.secrets.app.AUTH_SECRET' \
    '.secrets.app.SEARXNG_SECRET'; do
    VAL=$(yq "${path} // \"\"" "$VALUES_FILE" 2>/dev/null || true)
    if [ -z "$VAL" ] || [ "$VAL" = "null" ]; then
      CONFIG_COMPLETE=false
      CONFIG_MISSING="secrets"
      break
    fi
  done

  # Check for at least one LLM key or Ollama config
  if [ "$CONFIG_COMPLETE" = true ]; then
    HAS_LLM=false
    for key in \
      '.secrets.app.OPENROUTER_API_KEY' \
      '.secrets.app.OPENAI_API_KEY' \
      '.secrets.app.ANTHROPIC_API_KEY' \
      '.secrets.app.GOOGLE_AI_API_KEY'; do
      VAL=$(yq "${key} // \"\"" "$VALUES_FILE" 2>/dev/null || true)
      if [ -n "$VAL" ] && [ "$VAL" != "null" ]; then
        HAS_LLM=true
        break
      fi
    done
    # Also check if Ollama is configured
    OLLAMA_URL=$(yq '.config.OLLAMA_BASE_URL // ""' "$VALUES_FILE" 2>/dev/null || true)
    if [ -n "$OLLAMA_URL" ] && [ "$OLLAMA_URL" != "null" ]; then
      HAS_LLM=true
    fi
    if [ "$HAS_LLM" = false ]; then
      CONFIG_COMPLETE=false
      CONFIG_MISSING="LLM provider"
    fi
  fi
else
  CONFIG_COMPLETE=false
  CONFIG_MISSING="values file"
fi

if [ "$CONFIG_COMPLETE" = true ]; then
  echo "=== Step 4/5: Configure deployment ==="
  echo "  Secrets and LLM configuration found."
  if [ -t 0 ]; then
    printf "  Reconfigure? [y/N]: "
    read -r RECONFIG
    if [ "$RECONFIG" = "y" ] || [ "$RECONFIG" = "Y" ]; then
      bash ./scripts/configure-helm.sh
    else
      echo "  Keeping existing configuration."
    fi
  else
    echo "  Keeping existing configuration."
  fi
else
  echo "=== Step 4/5: Configure deployment (missing: ${CONFIG_MISSING}) ==="
  echo ""
  bash ./scripts/configure-helm.sh
fi
echo ""

# ---- Step 5: Deploy ----
echo "=== Step 5/5: Deploy with Helm ==="
echo ""

NAMESPACE="aurora"
RELEASE="aurora-oss"

# ── 5a. Ingress controller check ──
info "Checking for ingress controller..."
INGRESS_CLASSES=$(kubectl get ingressclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)

if [ -n "$INGRESS_CLASSES" ]; then
  ok "Ingress controller detected (classes: ${INGRESS_CLASSES})"

  CONFIGURED_CLASS=$(yq '.ingress.className // "nginx"' "$VALUES_FILE" 2>/dev/null || echo "nginx")
  if ! echo "$INGRESS_CLASSES" | grep -qw "$CONFIGURED_CLASS"; then
    warn "Configured ingress class '${CONFIGURED_CLASS}' not found in cluster."
    warn "Available classes: ${INGRESS_CLASSES}"
    if [ -t 0 ]; then
      FIRST_CLASS=$(echo "$INGRESS_CLASSES" | awk '{print $1}')
      printf "  Use '${FIRST_CLASS}' instead? [Y/n]: "
      read -r USE_DETECTED
      USE_DETECTED="${USE_DETECTED:-Y}"
      if [ "$USE_DETECTED" != "n" ] && [ "$USE_DETECTED" != "N" ]; then
        yq -i ".ingress.className = \"${FIRST_CLASS}\"" "$VALUES_FILE"
        ok "Set ingress.className = ${FIRST_CLASS}"
      fi
    fi
  fi
else
  warn "No ingress controller found in the cluster."
  echo ""
  echo "  Aurora needs an ingress controller to route traffic."
  echo "  Options:"
  echo "    1) Install nginx-ingress from the bundle (we'll do it now)"
  echo "    2) Skip — I'll set up an ingress controller myself"
  echo ""

  INSTALL_INGRESS="1"
  if [ -t 0 ]; then
    printf "  Choose [1]: "
    read -r INSTALL_INGRESS
    INSTALL_INGRESS="${INSTALL_INGRESS:-1}"
  fi

  if [ "$INSTALL_INGRESS" = "1" ]; then
    MANIFEST="./deploy/manifests/ingress-nginx-v1.8.1.yaml"
    if [ ! -f "$MANIFEST" ]; then
      echo "Error: ${MANIFEST} not found. Cannot install nginx-ingress without internet."
      echo "Either place the manifest at ${MANIFEST} or install an ingress controller manually."
      exit 1
    fi

    info "Installing nginx-ingress from local manifest (images from ${REGISTRY})..."
    sed "s|__REGISTRY__|${REGISTRY}|g" "$MANIFEST" | kubectl apply -f -
    info "Waiting for ingress controller to become ready..."
    kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s
    ok "Nginx Ingress Controller ready"

    yq -i '.ingress.className = "nginx"' "$VALUES_FILE"
  else
    warn "Skipping ingress controller install. Deploy one before Aurora will be reachable."
  fi
fi
echo ""

# ── 5b. Helm install ──
info "Running helm upgrade --install..."
helm upgrade --install "$RELEASE" ./deploy/helm/aurora \
  --namespace "$NAMESPACE" --create-namespace \
  --reset-values \
  -f "$VALUES_FILE"
ok "Helm release deployed"
echo ""

# ── 5c. Wait for pods ──
info "Waiting for pods..."
if ! kubectl rollout status deployment -n "$NAMESPACE" --timeout=180s 2>/dev/null; then
  warn "Some deployments did not become ready within 180s. Check pod status below."
fi
kubectl get pods -n "$NAMESPACE"
echo ""

# ── 5d. Detect ingress external IP ──
info "Checking for ingress external IP..."
EXTERNAL_IP=""

# First try: read from the Ingress resource status (works with any controller)
for i in $(seq 1 24); do
  EXTERNAL_IP=$(kubectl get ingress -n "$NAMESPACE" -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -z "$EXTERNAL_IP" ]; then
    EXTERNAL_IP=$(kubectl get ingress -n "$NAMESPACE" -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  fi
  if [ -n "$EXTERNAL_IP" ]; then
    break
  fi
  sleep 5
done

if [ -n "$EXTERNAL_IP" ]; then
  ok "Ingress external IP: $EXTERNAL_IP"
else
  warn "Could not detect ingress IP automatically."
  echo "  Your platform team may need to provision a load balancer or assign a static IP."
  echo "  Check with:  kubectl get ingress -n $NAMESPACE"
  if [ -t 0 ]; then
    printf "  Enter IP/hostname manually (or press Enter to skip): "
    read -r EXTERNAL_IP
  fi
fi
echo ""

if [ -n "$EXTERNAL_IP" ]; then
  FRONTEND_HOST=$(yq '.ingress.hosts.frontend // "aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "aurora.example.com")
  API_HOST=$(yq '.ingress.hosts.api // "api.aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "api.aurora.example.com")
  WS_HOST=$(yq '.ingress.hosts.ws // "ws.aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "ws.aurora.example.com")
  echo "  Point your DNS records to this IP:"
  echo ""
  echo "    ${FRONTEND_HOST}  →  A  ${EXTERNAL_IP}"
  echo "    ${API_HOST}       →  A  ${EXTERNAL_IP}"
  echo "    ${WS_HOST}        →  A  ${EXTERNAL_IP}"
  echo ""
fi

# ── 5e. Vault setup (first deploy only) ──
VAULT_TOKEN_VAL=$(yq '.secrets.backend.VAULT_TOKEN // ""' "$VALUES_FILE" 2>/dev/null || true)

if [ -n "$VAULT_TOKEN_VAL" ] && [ "$VAULT_TOKEN_VAL" != "null" ]; then
  ok "Vault token already configured — skipping Vault setup"
else
  info "Setting up Vault (first deploy)..."

  info "Waiting for Vault pod..."
  until kubectl get pod/${RELEASE}-vault-0 -n "$NAMESPACE" &>/dev/null; do
    sleep 2
  done
  kubectl wait --for=condition=Ready=false pod/${RELEASE}-vault-0 -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
  sleep 3

  info "Initializing Vault..."
  VAULT_INIT=$(kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- vault operator init -key-shares=1 -key-threshold=1 2>&1)
  UNSEAL_KEY=$(echo "$VAULT_INIT" | grep "Unseal Key 1:" | awk '{print $NF}')
  ROOT_TOKEN=$(echo "$VAULT_INIT" | grep "Initial Root Token:" | awk '{print $NF}')

  if [ -z "$UNSEAL_KEY" ] || [ -z "$ROOT_TOKEN" ]; then
    warn "Vault may already be initialized. Check manually:"
    echo "  kubectl -n $NAMESPACE exec -it statefulset/${RELEASE}-vault -- vault status"
  else
    ok "Vault initialized"

    CREDENTIALS_FILE="vault-init-${RELEASE}.txt"
    (umask 077 && cat > "$CREDENTIALS_FILE" <<CEOF
Unseal Key: $UNSEAL_KEY
Root Token: $ROOT_TOKEN
CEOF
    )
    warn "Vault credentials written to $CREDENTIALS_FILE (mode 600). Store securely and delete after use."

    info "Unsealing Vault..."
    kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- vault operator unseal "$UNSEAL_KEY" >/dev/null 2>&1
    ok "Vault unsealed"

    info "Configuring Vault KV engine..."
    kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && echo \"$ROOT_TOKEN\" | vault login - >/dev/null 2>&1"
    kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2 >/dev/null 2>&1" || true
    kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- sh -c 'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - >/dev/null 2>&1 <<EOF
path "aurora/data/users/*" { capabilities = ["create","read","update","delete","list"] }
path "aurora/metadata/users/*" { capabilities = ["list","read","delete"] }
path "aurora/metadata/" { capabilities = ["list"] }
path "aurora/metadata/users" { capabilities = ["list"] }
EOF'

    APP_TOKEN=$(kubectl -n "$NAMESPACE" exec statefulset/${RELEASE}-vault -- sh -c "export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0 -format=json 2>/dev/null" | python3 -c "import sys,json; print(json.load(sys.stdin)['auth']['client_token'])")
    ok "Vault configured: app token created"

    info "Updating values with Vault token and redeploying..."
    yq -i ".secrets.backend.VAULT_TOKEN = \"$APP_TOKEN\"" "$VALUES_FILE"
    helm upgrade "$RELEASE" ./deploy/helm/aurora --reset-values -f "$VALUES_FILE" -n "$NAMESPACE"
    if ! kubectl rollout status deployment -n "$NAMESPACE" --timeout=120s 2>/dev/null; then
      warn "Some deployments did not become ready after Vault redeploy. Check pod status."
    fi
    ok "Redeployed with Vault token"
  fi
fi

# ── Summary ──
echo ""
echo "═══════════════════════════════════════════════"
echo "  Aurora deployment complete!"
echo "═══════════════════════════════════════════════"
echo ""
kubectl get pods -n "$NAMESPACE"
echo ""

if [ -n "$EXTERNAL_IP" ]; then
  FRONTEND_HOST=$(yq '.ingress.hosts.frontend // "aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "aurora.example.com")
  API_HOST=$(yq '.ingress.hosts.api // "api.aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "api.aurora.example.com")
  WS_HOST=$(yq '.ingress.hosts.ws // "ws.aurora.example.com"' "$VALUES_FILE" 2>/dev/null || echo "ws.aurora.example.com")
  SCHEME="https"
  echo "  Frontend:  ${SCHEME}://${FRONTEND_HOST}"
  echo "  API:       ${SCHEME}://${API_HOST}/health/"
  echo "  WebSocket: wss://${WS_HOST}"
  echo ""
  echo "  Ingress IP: ${EXTERNAL_IP}"
  echo ""
fi

echo "Post-deploy:"
echo "  (Recommended) Set up KMS auto-unseal so Vault auto-unseals on pod restarts:"
echo "  See docs/deployment/vault-kms.md"
echo ""
