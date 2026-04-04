#!/usr/bin/env bash
set -euo pipefail

# Adaptive Aurora Kubernetes deployment.
#
# Detects what's available (internet, tarball, registry, cluster access) and
# does what it can. When a step can't be completed, it prints clear next steps
# and exits cleanly.
#
# Usage:
#   ./scripts/deploy-k8s.sh <registry-url>
#   ./scripts/deploy-k8s.sh <registry-url> <version>
#   ./scripts/deploy-k8s.sh <registry-url> --tarball <path>
#   ./scripts/deploy-k8s.sh <registry-url> --skip-push
#
# Example:
#   ./scripts/deploy-k8s.sh registry.internal:5000
#   ./scripts/deploy-k8s.sh registry.internal:5000 v1.2.3
#   ./scripts/deploy-k8s.sh registry.internal:5000 --tarball aurora-airtight-v1.2.3-amd64.tar.gz
#
# Can also be run via curl on a fresh machine:
#   curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/deploy-k8s.sh | bash -s -- <registry-url>

REGISTRY=""
VERSION="latest"
TARBALL_FLAG=""
SKIP_PUSH=false

while [ $# -gt 0 ]; do
  case "$1" in
    --tarball)
      TARBALL_FLAG="${2:-}"
      [ -z "$TARBALL_FLAG" ] && { echo "Error: --tarball requires a path"; exit 1; }
      shift 2
      ;;
    --skip-push)
      SKIP_PUSH=true
      shift
      ;;
    -h|--help)
      echo "Usage: $0 <registry-url> [version] [--tarball <path>] [--skip-push]"
      echo ""
      echo "  registry-url    Target registry (e.g. registry.internal:5000)"
      echo "  version         Aurora version (default: latest release)"
      echo "  --tarball PATH  Use a specific airgap tarball for image push"
      echo "  --skip-push     Skip image push (images already in registry)"
      exit 0
      ;;
    *)
      if [ -z "$REGISTRY" ]; then
        REGISTRY="$1"
      elif [ "$VERSION" = "latest" ] && [[ "$1" =~ ^v[0-9] ]]; then
        VERSION="$1"
      else
        echo "Error: unexpected argument '$1'"
        echo "Usage: $0 <registry-url> [version] [--tarball <path>] [--skip-push]"
        exit 1
      fi
      shift
      ;;
  esac
done

if [ -z "$REGISTRY" ]; then
  echo "Usage: $0 <registry-url> [version] [--tarball <path>] [--skip-push]"
  exit 1
fi

REGISTRY="${REGISTRY%/}"

echo "============================================"
echo "  Aurora Kubernetes Deployment"
echo "  Registry: $REGISTRY"
echo "============================================"
echo ""

# ── Helpers ──────────────────────────────────────────────────────────────────

info() { echo -e "\033[1;34m→\033[0m $1"; }
ok()   { echo -e "\033[1;32m✓\033[0m $1"; }
warn() { echo -e "\033[1;33m!\033[0m $1"; }

check_tool() {
  if ! command -v "$1" &>/dev/null; then
    return 1
  fi
  return 0
}

# ── Pre-flight ───────────────────────────────────────────────────────────────

MISSING=""
for cmd in helm yq openssl python3; do
  check_tool "$cmd" || MISSING="$MISSING $cmd"
done

if ! check_tool skopeo && ! check_tool docker; then
  MISSING="$MISSING skopeo-or-docker"
fi

if [ -n "$MISSING" ]; then
  echo "Missing required tools:$MISSING"
  exit 1
fi

HAS_INTERNET=false
if curl -fsSL --connect-timeout 5 --max-time 10 "https://ghcr.io/v2/" >/dev/null 2>&1; then
  HAS_INTERNET=true
fi

HAS_KUBECTL=false
if check_tool kubectl && kubectl cluster-info &>/dev/null 2>&1; then
  HAS_KUBECTL=true
fi

# ── Step 1/5: Resolve version & source ───────────────────────────────────────

echo "=== Step 1/5: Resolve version & source ==="
echo ""

IN_REPO=false
if [ -d "deploy/helm/aurora" ] && [ -d "scripts" ]; then
  IN_REPO=true
fi

TARGET_VERSION="$VERSION"
TARBALL=""

if [ -n "$TARBALL_FLAG" ]; then
  if [ ! -f "$TARBALL_FLAG" ]; then
    echo "Error: tarball not found: $TARBALL_FLAG"
    exit 1
  fi
  TARBALL="$TARBALL_FLAG"
  if [ "$TARGET_VERSION" = "latest" ]; then
    TARGET_VERSION=$(echo "$TARBALL" | sed 's/.*aurora-airtight-\(.*\)-[a-z0-9]*\.tar\.gz/\1/')
  fi
  ok "Using tarball: $TARBALL (version: $TARGET_VERSION)"
elif [ "$IN_REPO" = true ] && [ "$HAS_INTERNET" = true ]; then
  ok "Running from repo with internet access — will pull images directly"
  if [ "$TARGET_VERSION" = "latest" ]; then
    LATEST=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/arvo-ai/aurora/releases/latest" 2>/dev/null | grep '"tag_name"' | cut -d'"' -f4 || true)
    [ -n "$LATEST" ] && TARGET_VERSION="$LATEST"
  fi
else
  # Look for local tarball
  for f in aurora-airtight-*.tar.gz; do
    [ -f "$f" ] || continue
    TARBALL="$f"
    break
  done

  if [ -n "$TARBALL" ]; then
    if [ "$TARGET_VERSION" = "latest" ]; then
      TARGET_VERSION=$(echo "$TARBALL" | sed 's/.*aurora-airtight-\(.*\)-[a-z0-9]*\.tar\.gz/\1/')
    fi
    ok "Found local tarball: $TARBALL (version: $TARGET_VERSION)"
  elif [ "$HAS_INTERNET" = true ]; then
    info "No local tarball, but internet available — downloading..."
    if [ "$TARGET_VERSION" = "latest" ]; then
      LATEST=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/arvo-ai/aurora/releases/latest" 2>/dev/null | grep '"tag_name"' | cut -d'"' -f4 || true)
      [ -n "$LATEST" ] && TARGET_VERSION="$LATEST"
    fi
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd 2>/dev/null || pwd)"
    if [ -f "$SCRIPT_DIR/download-bundle.sh" ]; then
      bash "$SCRIPT_DIR/download-bundle.sh" "$TARGET_VERSION"
    else
      curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash -s -- "$TARGET_VERSION"
    fi
    TARBALL=$(ls aurora-airtight-*.tar.gz 2>/dev/null | head -1 || true)
    ok "Downloaded: $TARBALL"
  else
    echo ""
    warn "No internet access and no local tarball found."
    echo ""
    echo "  To proceed, do one of the following:"
    echo ""
    echo "  1. Download the bundle on a machine with internet:"
    echo "     curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash"
    echo ""
    echo "  2. Transfer both files here, then re-run:"
    echo "     $0 $REGISTRY --tarball aurora-airtight-<version>-<arch>.tar.gz"
    exit 1
  fi
fi

echo "  Version: $TARGET_VERSION"
echo ""

# ── Step 2/5: Ensure source (Helm chart + scripts) ──────────────────────────

echo "=== Step 2/5: Ensure source (Helm chart + scripts) ==="
echo ""

REPO_DIR="."

if [ "$IN_REPO" = true ]; then
  ok "Already in repo — Helm chart and scripts available"
else
  REPO_DIR=$(find . -maxdepth 1 -type d -name "aurora-*" ! -name "aurora-airtight-*" | head -1 || true)

  if [ -n "$REPO_DIR" ] && [ -d "$REPO_DIR/deploy/helm" ]; then
    ok "Found extracted source: $REPO_DIR"
  else
    SOURCE_ARCHIVE=$(ls aurora-*.tar.gz 2>/dev/null | grep -v airtight | head -1 || true)
    if [ -n "$SOURCE_ARCHIVE" ]; then
      info "Extracting $SOURCE_ARCHIVE..."
      tar xzf "$SOURCE_ARCHIVE"
      REPO_DIR=$(tar tzf "$SOURCE_ARCHIVE" | head -1 | cut -d/ -f1)
      ok "Extracted to $REPO_DIR"
    elif [ "$HAS_INTERNET" = true ]; then
      info "Downloading source archive..."
      VERSION_TAG="${TARGET_VERSION}"
      VERSION_STRIPPED="${TARGET_VERSION#v}"
      curl -fsSL -o "aurora-${VERSION_STRIPPED}.tar.gz" "https://github.com/arvo-ai/aurora/archive/refs/tags/${VERSION_TAG}.tar.gz"
      tar xzf "aurora-${VERSION_STRIPPED}.tar.gz"
      REPO_DIR="aurora-${VERSION_STRIPPED}"
      ok "Downloaded and extracted to $REPO_DIR"
    else
      warn "Source archive not found and no internet to download it."
      echo "  Place aurora-<version>.tar.gz in the current directory and re-run."
      exit 1
    fi
  fi
  cd "$REPO_DIR"
fi
echo ""

# ── Step 3/5: Push images to registry ────────────────────────────────────────

echo "=== Step 3/5: Push images to registry ==="
echo ""

VALUES_FILE="deploy/helm/aurora/values.generated.yaml"

if [ "$SKIP_PUSH" = true ]; then
  ok "Skipping image push (--skip-push)"
else
  CURRENT_REGISTRY=""
  if [ -f "$VALUES_FILE" ]; then
    CURRENT_REGISTRY=$(yq '.image.registry // ""' "$VALUES_FILE" 2>/dev/null || true)
  fi

  if [ -n "$CURRENT_REGISTRY" ] && [ "$CURRENT_REGISTRY" = "$REGISTRY" ]; then
    ok "Images already pushed to $REGISTRY (re-run with a fresh values file to force)"
  else
    PUSH_ARGS="$REGISTRY"
    if [ -n "$TARBALL" ]; then
      PUSH_ARGS="$REGISTRY --tarball $TARBALL"
    fi
    bash ./scripts/push-to-registry.sh $PUSH_ARGS
  fi
fi
echo ""

# ── Step 4/5: Configure ─────────────────────────────────────────────────────

echo "=== Step 4/5: Configure deployment ==="
echo ""

CONFIG_COMPLETE=true
if [ -f "$VALUES_FILE" ]; then
  for path in \
    '.secrets.db.POSTGRES_PASSWORD' \
    '.secrets.db.MEMGRAPH_PASSWORD' \
    '.secrets.app.FLASK_SECRET_KEY' \
    '.secrets.app.AUTH_SECRET' \
    '.secrets.app.SEARXNG_SECRET'; do
    VAL=$(yq "${path} // \"\"" "$VALUES_FILE" 2>/dev/null || true)
    if [ -z "$VAL" ] || [ "$VAL" = "null" ]; then
      CONFIG_COMPLETE=false
      break
    fi
  done
else
  CONFIG_COMPLETE=false
fi

if [ "$CONFIG_COMPLETE" = true ]; then
  ok "Configuration found in $VALUES_FILE"
  if [ -t 0 ]; then
    printf "  Reconfigure? [y/N]: "
    read -r RECONFIG
    if [ "$RECONFIG" = "y" ] || [ "$RECONFIG" = "Y" ]; then
      bash ./scripts/configure-helm.sh
    else
      echo "  Keeping existing configuration."
    fi
  fi
else
  if [ -t 0 ]; then
    bash ./scripts/configure-helm.sh
  else
    warn "Non-interactive mode — generating secrets only."
    bash ./scripts/configure-helm.sh --non-interactive
    echo "  Edit ${VALUES_FILE} to configure LLM keys and domain, then re-run."
  fi
fi
echo ""

# ── Step 5/5: Deploy to cluster ─────────────────────────────────────────────

echo "=== Step 5/5: Deploy to cluster ==="
echo ""

if [ "$HAS_KUBECTL" != true ]; then
  warn "Cannot reach Kubernetes cluster (kubectl not available or cluster unreachable)."
  echo ""
  echo "  Steps 1-4 are complete. To finish deployment from a machine with cluster access:"
  echo ""
  if [ "$IN_REPO" = true ]; then
    echo "    # From this directory:"
    echo "    $0 $REGISTRY --skip-push"
  else
    echo "    # Transfer the repo and values file to your bastion/jump host, then:"
    echo "    cd $(basename "$PWD")"
    echo "    $0 $REGISTRY --skip-push"
  fi
  echo ""
  echo "  Or deploy manually:"
  echo "    helm upgrade --install aurora-oss ./deploy/helm/aurora \\"
  echo "      --namespace aurora --create-namespace --reset-values \\"
  echo "      -f $VALUES_FILE"
  exit 0
fi

NAMESPACE="aurora"
RELEASE="aurora-oss"

# ── 5a. Ingress controller ──
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
      echo "Error: ${MANIFEST} not found."
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

for i in $(seq 1 24); do
  EXTERNAL_IP=$(kubectl get ingress -n "$NAMESPACE" -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
  if [ -z "$EXTERNAL_IP" ]; then
    EXTERNAL_IP=$(kubectl get ingress -n "$NAMESPACE" -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  fi
  [ -n "$EXTERNAL_IP" ] && break
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

# ── Summary ──────────────────────────────────────────────────────────────────

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
  TLS_ENABLED=$(yq '.ingress.tls.enabled // false' "$VALUES_FILE" 2>/dev/null || echo "false")
  if [ "$TLS_ENABLED" = "true" ]; then
    SCHEME="https"; WS_SCHEME="wss"
  else
    SCHEME="http"; WS_SCHEME="ws"
  fi
  echo "  Frontend:  ${SCHEME}://${FRONTEND_HOST}"
  echo "  API:       ${SCHEME}://${API_HOST}/health/"
  echo "  WebSocket: ${WS_SCHEME}://${WS_HOST}"
  echo ""
  echo "  Ingress IP: ${EXTERNAL_IP}"
  echo ""
fi

echo "Post-deploy:"
echo "  (Recommended) Set up KMS auto-unseal so Vault auto-unseals on pod restarts:"
echo "  See docs/deployment/vault-kms-setup.md"
echo ""
