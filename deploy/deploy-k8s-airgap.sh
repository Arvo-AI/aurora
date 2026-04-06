#!/usr/bin/env bash
set -euo pipefail

# Aurora Kubernetes deployment.
#
# Interactively guides the user through the right deployment path based on
# their environment (standard vs air-gapped, workstation vs bastion).
#
# Usage:
#   ./deploy/deploy-k8s-airgap.sh <registry-url>
#   ./deploy/deploy-k8s-airgap.sh <registry-url> <version>
#   ./deploy/deploy-k8s-airgap.sh <registry-url> --tarball <path>
#   ./deploy/deploy-k8s-airgap.sh <registry-url> --skip-push
#
# Example:
#   ./deploy/deploy-k8s-airgap.sh registry.internal:5000
#   ./deploy/deploy-k8s-airgap.sh registry.internal:5000 v1.2.3
#   ./deploy/deploy-k8s-airgap.sh registry.internal:5000 --tarball aurora-airtight-v1.2.3-amd64.tar.gz

REGISTRY=""
VERSION="latest"
TARBALL_FLAG=""
SKIP_PUSH=false
DEPLOY_MODE_FLAG=""

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
    --mode)
      DEPLOY_MODE_FLAG="${2:-}"
      [ -z "$DEPLOY_MODE_FLAG" ] && { echo "Error: --mode requires a value (standard|bastion|prepare)"; exit 1; }
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 <registry-url> [version] [--tarball <path>] [--skip-push] [--mode standard|bastion|prepare]"
      echo ""
      echo "  registry-url    Target registry (e.g. registry.internal:5000)"
      echo "  version         Aurora version (default: latest release)"
      echo "  --tarball PATH  Use a specific airgap tarball for image push"
      echo "  --skip-push     Skip image push (images already in registry)"
      echo "  --mode MODE     Skip the interactive menu (standard, bastion, or prepare)"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "$SCRIPT_DIR/lib/helpers.sh"

_build_resume_cmd() {
  local cmd="./deploy/deploy-k8s-airgap.sh ${REGISTRY}"
  [ "${VERSION}" != "latest" ] && cmd="$cmd ${VERSION}"
  [ -n "${DEPLOY_MODE:-}" ] && cmd="$cmd --mode ${DEPLOY_MODE}"
  [ -n "${TARBALL_FLAG:-}" ] && cmd="$cmd --tarball ${TARBALL_FLAG}"
  [ "${SKIP_PUSH:-false}" = true ] && cmd="$cmd --skip-push"
  echo "$cmd"
}

_print_resume_hint() {
  echo ""
  echo "───────────────────────────────────────────"
  echo "  It's safe to re-run — completed steps will be detected and skipped."
  echo "  Resume with the same settings:"
  echo ""
  echo "    $(_build_resume_cmd)"
  echo ""
  echo "───────────────────────────────────────────"
}

_on_error() {
  local exit_code=$?
  echo ""
  echo "  The script hit an unexpected error (exit code ${exit_code})."
  _print_resume_hint
}

trap '_on_error' ERR

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

# ── Detect environment ────────────────────────────────────────────────────────

HAS_INTERNET=false
if curl -sS --connect-timeout 5 --max-time 10 -o /dev/null "https://ghcr.io/v2/" 2>/dev/null; then
  HAS_INTERNET=true
fi

HAS_KUBECTL=false
if check_tool kubectl && kubectl cluster-info &>/dev/null 2>&1; then
  HAS_KUBECTL=true
fi

IN_REPO=false
if [ -d "deploy/helm/aurora" ] && [ -d "scripts" ]; then
  IN_REPO=true
fi

# ── Choose deployment path ────────────────────────────────────────────────────

DEPLOY_MODE=""

if [ -n "$DEPLOY_MODE_FLAG" ]; then
  DEPLOY_MODE="$DEPLOY_MODE_FLAG"
elif [ -n "$TARBALL_FLAG" ]; then
  DEPLOY_MODE="bastion"
elif [ "$SKIP_PUSH" = true ]; then
  DEPLOY_MODE="bastion"
elif [ -t 0 ]; then
  select_menu "How would you like to deploy Aurora?" \
    "Standard — this machine has internet and can reach the cluster" \
    "Air-gapped — I'm on the bastion / jump host with cluster access" \
    "Prepare bundle — download files to transfer to an air-gapped environment"

  case "$MENU_RESULT" in
    0) DEPLOY_MODE="standard" ;;
    1) DEPLOY_MODE="bastion" ;;
    2) DEPLOY_MODE="prepare" ;;
  esac
else
  if [ "$HAS_INTERNET" = true ] && [ "$HAS_KUBECTL" = true ]; then
    DEPLOY_MODE="standard"
  else
    DEPLOY_MODE="bastion"
  fi
fi

echo ""

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: prepare — Download bundle for transfer to air-gapped environment
# ═══════════════════════════════════════════════════════════════════════════════

if [ "$DEPLOY_MODE" = "prepare" ]; then
  if [ "$HAS_INTERNET" != true ]; then
    warn "This machine doesn't appear to have internet access."
    echo "  Option 3 is for downloading files on a connected machine."
    echo "  If you're on the bastion, choose option 2 instead."
    exit 1
  fi

  info "Downloading Aurora bundle for air-gapped transfer..."
  echo ""

  TARGET_VERSION="$VERSION"
  if [ "$TARGET_VERSION" = "latest" ]; then
    LATEST=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/arvo-ai/aurora/releases/latest" 2>/dev/null | grep '"tag_name"' | cut -d'"' -f4 || true)
    [ -n "$LATEST" ] && TARGET_VERSION="$LATEST"
  fi

  if [ -f "$SCRIPT_DIR/download-bundle.sh" ]; then
    bash "$SCRIPT_DIR/download-bundle.sh" "$TARGET_VERSION"
  else
    curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/download-bundle.sh | bash -s -- "$TARGET_VERSION"
  fi

  echo ""
  echo "═══════════════════════════════════════════════"
  echo "  Bundle downloaded. Next steps:"
  echo "═══════════════════════════════════════════════"
  echo ""
  echo "  1. Transfer the following files to your bastion / jump host:"
  echo ""
  ls -1 aurora-airtight-*.tar.gz aurora-*.tar.gz 2>/dev/null | grep -v "\.sha256" | sed 's/^/     /'
  echo ""
  echo "     Example:  scp aurora-*.tar.gz bastion:/tmp/"
  echo ""
  echo "  2. On the bastion, extract the source and run:"
  echo ""
  echo "     tar xzf aurora-*.tar.gz  # (the smaller one — source archive)"
  echo "     cd aurora-*/"
  echo "     ./deploy/deploy-k8s-airgap.sh ${REGISTRY}"
  echo "     # Choose option 2 (air-gapped bastion)"
  echo ""
  exit 0
fi

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: standard / bastion — Full deployment
# ═══════════════════════════════════════════════════════════════════════════════

# ── Step 1: Resolve version & source ──────────────────────────────────────────

echo "=== Step 1: Resolve version & source ==="
echo ""

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

elif [ "$DEPLOY_MODE" = "standard" ]; then
  if [ "$IN_REPO" = true ]; then
    ok "Running from repo with internet access — will pull images directly"
  fi
  if [ "$TARGET_VERSION" = "latest" ]; then
    LATEST=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/arvo-ai/aurora/releases/latest" 2>/dev/null | grep '"tag_name"' | cut -d'"' -f4 || true)
    [ -n "$LATEST" ] && TARGET_VERSION="$LATEST"
  fi

elif [ "$DEPLOY_MODE" = "bastion" ]; then
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
  elif [ "$SKIP_PUSH" = true ]; then
    ok "Skipping image push — no tarball needed"
  else
    warn "No image tarball found in the current directory."
    echo ""
    echo "  Expected: aurora-airtight-<version>-<arch>.tar.gz"
    echo ""
    echo "  Place the tarball in the current directory and re-run,"
    echo "  or follow the manual steps in the docs:"
    echo "  https://docs.arvo.ai/deployment/kubernetes-airgap"
    _print_resume_hint
    exit 1
  fi
fi

echo "  Version: $TARGET_VERSION"
echo ""

# ── Step 2/5: Ensure source (Helm chart + scripts) ──────────────────────────

echo "=== Step 2: Ensure source (Helm chart + scripts) ==="
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
      _print_resume_hint
      exit 1
    fi
  fi
  cd "$REPO_DIR"
fi
echo ""

# ── Step 3/5: Push images to registry ────────────────────────────────────────

echo "=== Step 3: Push images to registry ==="
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
    bash ./deploy/push-to-registry.sh $PUSH_ARGS
  fi
fi
echo ""

# ── Step 4/5: Configure ─────────────────────────────────────────────────────

echo "=== Step 4: Configure deployment ==="
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
      bash ./deploy/configure-helm.sh
    else
      echo "  Keeping existing configuration."
    fi
  fi
else
  if [ -t 0 ]; then
    bash ./deploy/configure-helm.sh
  else
    warn "Non-interactive mode — generating secrets only."
    bash ./deploy/configure-helm.sh --non-interactive
    echo "  Edit ${VALUES_FILE} to configure LLM keys and domain, then re-run."
  fi
fi
echo ""

# ── Step 5/5: Deploy to cluster ─────────────────────────────────────────────

echo "=== Step 5: Deploy to cluster ==="
echo ""

if [ "$HAS_KUBECTL" != true ]; then
  echo ""
  warn "Cannot reach Kubernetes cluster from this machine."
  echo ""
  if [ "$DEPLOY_MODE" = "bastion" ]; then
    echo "  You selected the bastion/air-gap option, but kubectl can't reach the cluster."
    echo "  Make sure kubectl is configured and the cluster is reachable, then re-run."
    _print_resume_hint
  else
    echo "  Steps 1–4 are complete. To finish, run the Helm install on a machine"
    echo "  with cluster access (e.g., your bastion or jump host):"
    echo ""
    echo "    helm upgrade --install aurora-oss ./deploy/helm/aurora \\"
    echo "      --namespace aurora --create-namespace --reset-values \\"
    echo "      -f $VALUES_FILE"
    echo ""
    echo "  Transfer the source directory and $VALUES_FILE to that machine."
    echo "  Then continue with Vault setup — see the deployment docs."
    echo ""
    echo "  Or, if the issue is just kubectl connectivity, fix it and re-run:"
    _print_resume_hint
  fi
  exit 1
fi

NAMESPACE="aurora"
RELEASE="aurora-oss"

# ── 5a. Ingress controller ──
info "Checking for ingress controller..."
INGRESS_CLASSES=$(kubectl get ingressclass -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
INGRESS_NGINX_EXISTS=$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o name 2>/dev/null || true)

if [ -n "$INGRESS_CLASSES" ]; then
  ok "Ingress controller detected (classes: ${INGRESS_CLASSES})"

  if [ -n "$INGRESS_NGINX_EXISTS" ]; then
    READY=$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    if [ "${READY:-0}" = "0" ]; then
      info "nginx-ingress deployment exists but not yet ready — waiting (up to 180s)..."
      if kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=180s 2>/dev/null; then
        ok "Nginx Ingress Controller ready"
      else
        warn "Ingress controller still not ready after 180s."
        echo "  The admission webhook may block Helm installs until the controller is up."
        echo "  Deleting the webhook so the deployment can proceed..."
        kubectl delete validatingwebhookconfiguration ingress-nginx-admission 2>/dev/null || true
        echo "  (The controller will recreate it once it's running.)"
      fi
    fi
  fi

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
      _print_resume_hint
      exit 1
    fi

    info "Installing nginx-ingress from local manifest (images from ${REGISTRY})..."
    sed "s|__REGISTRY__|${REGISTRY}|g" "$MANIFEST" | kubectl apply -f -
    info "Waiting for ingress controller to become ready (up to 120s)..."
    if kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s 2>/dev/null; then
      ok "Nginx Ingress Controller ready"
    else
      warn "Ingress controller not ready within 120s."
      echo "  Deleting the admission webhook so Helm can proceed..."
      kubectl delete validatingwebhookconfiguration ingress-nginx-admission 2>/dev/null || true
      echo "  (The controller will recreate it once it's running.)"
    fi

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
