#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Deployment Wizard
# ─────────────────────────────────────────────────────────────────────────────
# Interactive deployment with profile selection, preflight checks, automatic
# Vault token extraction, and post-deploy health verification.
#
# Usage:
#   ./deploy/aurora-deploy.sh                            # interactive
#   ./deploy/aurora-deploy.sh --profile standard         # standard VM deploy
#   ./deploy/aurora-deploy.sh --profile airtight         # air-tight / offline deploy
#   ./deploy/aurora-deploy.sh --build                    # build from source (default)
#   ./deploy/aurora-deploy.sh --prebuilt                 # pull prebuilt images
#   ./deploy/aurora-deploy.sh --skip-docker              # skip Docker installation
#   ./deploy/aurora-deploy.sh --skip-firewall            # skip firewall setup
#   ./deploy/aurora-deploy.sh --hostname <host>          # set hostname/IP
#   ./deploy/aurora-deploy.sh --bundle <path>            # airtight bundle path
#   ./deploy/aurora-deploy.sh --non-interactive          # no prompts (requires env vars)
#
# Non-interactive env vars: LLM_API_KEY, LLM_PROVIDER, VM_HOSTNAME (or --hostname)
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source shared libraries
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/vault.sh"
source "$SCRIPT_DIR/lib/health.sh"

# ─── Defaults ────────────────────────────────────────────────────────────────

PROFILE=""
BUILD_MODE="build"
SKIP_DOCKER=false
SKIP_FIREWALL=false
NON_INTERACTIVE=false
VM_HOSTNAME=""
AIRTIGHT_BUNDLE=""
VERSION="${VERSION:-latest}"

# ─── Parse args ──────────────────────────────────────────────────────────────

_shift_next=""
for arg in "$@"; do
  if [[ -n "$_shift_next" ]]; then
    printf -v "$_shift_next" '%s' "$arg"
    _shift_next=""
    continue
  fi
  case $arg in
    --profile=*)        PROFILE="${arg#*=}" ;;
    --profile)          _shift_next=PROFILE ;;
    --prebuilt)         BUILD_MODE="prebuilt" ;;
    --build)            BUILD_MODE="build" ;;
    --skip-docker)      SKIP_DOCKER=true ;;
    --skip-firewall)    SKIP_FIREWALL=true ;;
    --non-interactive)  NON_INTERACTIVE=true ;;
    --hostname=*)       VM_HOSTNAME="${arg#*=}" ;;
    --hostname)         _shift_next=VM_HOSTNAME ;;
    --bundle=*)         AIRTIGHT_BUNDLE="${arg#*=}" ;;
    --bundle)           _shift_next=AIRTIGHT_BUNDLE ;;
    -h|--help)
      sed -n '3,/^# ──/{ /^#/s/^# \?//p }' "$0"
      exit 0
      ;;
    *) err "Unknown argument: $arg"; exit 1 ;;
  esac
done

# ─── Banner ──────────────────────────────────────────────────────────────────

[[ -f "$REPO_ROOT/scripts/show-logo.sh" ]] && bash "$REPO_ROOT/scripts/show-logo.sh" 2>/dev/null || true

echo ""
echo "======================================================="
echo "  Aurora Deployment Wizard"
echo "======================================================="
echo ""

# ─── Profile selection ───────────────────────────────────────────────────────

if [[ -z "$PROFILE" ]]; then
  if [[ "$NON_INTERACTIVE" == "true" ]]; then
    PROFILE="standard"
  else
    info "Select deployment profile:"
    echo ""
    echo "  [1] Standard Production"
    echo "      Internet-connected VM. Build from source or pull prebuilt images."
    echo ""
    echo "  [2] Air-Tight Enterprise"
    echo "      Restricted-egress VM. Load images from a pre-transferred bundle."
    echo ""
    prompt PROFILE_CHOICE "Enter 1 or 2" "1"
    case "$PROFILE_CHOICE" in
      1|standard)  PROFILE="standard" ;;
      2|airtight)  PROFILE="airtight" ;;
      *) err "Invalid selection."; exit 1 ;;
    esac
  fi
fi

# Validate profile
case "$PROFILE" in
  standard|airtight) ;;
  *) err "Unknown profile: $PROFILE (use 'standard' or 'airtight')"; exit 1 ;;
esac

echo ""
ok "Profile: $PROFILE"
echo ""

# ─── Prerequisites & Preflight ───────────────────────────────────────────────

ensure_prerequisites
preflight

# ─── Profile: Air-Tight bundle validation ────────────────────────────────────

COMPOSE_FILE="docker-compose.prod-local.yml"

if [[ "$PROFILE" == "airtight" ]]; then
  COMPOSE_FILE="docker-compose.airtight.yml"

  if [[ -z "$AIRTIGHT_BUNDLE" ]]; then
    prompt AIRTIGHT_BUNDLE "Path to airtight image bundle (.tar.gz)"
  fi

  # Expand ~ to home dir
  AIRTIGHT_BUNDLE="${AIRTIGHT_BUNDLE/#\~/$HOME}"

  if [[ ! -f "$AIRTIGHT_BUNDLE" ]]; then
    err "Bundle not found: $AIRTIGHT_BUNDLE"
    exit 1
  fi
  ok "Bundle found: $AIRTIGHT_BUNDLE"

  # Check checksum if .sha256 file exists alongside
  local_sha="${AIRTIGHT_BUNDLE}.sha256"
  if [[ -f "$local_sha" ]]; then
    info "Verifying bundle integrity..."
    if sha256sum -c "$local_sha" &>/dev/null || shasum -a 256 -c "$local_sha" &>/dev/null; then
      ok "Checksum verified"
    else
      warn "Checksum mismatch -- bundle may be corrupted"
      confirm "Continue anyway?" || exit 1
    fi
  fi
fi

# ─── Docker install (standard only) ─────────────────────────────────────────

if [[ "$PROFILE" == "standard" ]]; then
  if [[ "$SKIP_DOCKER" == "true" ]]; then
    warn "Skipping Docker installation (--skip-docker)"
  else
    install_docker
  fi
fi

# ─── Firewall ────────────────────────────────────────────────────────────────

if [[ "$SKIP_FIREWALL" == "true" ]]; then
  warn "Skipping firewall setup (--skip-firewall)"
else
  configure_firewall
fi

# ─── Hostname / IP ───────────────────────────────────────────────────────────

echo ""
info "Detecting public IP address..."
DETECTED_IP=$(detect_ip)
[[ -n "$DETECTED_IP" ]] && ok "Detected IP: $DETECTED_IP" || warn "Could not auto-detect public IP."

if [[ -z "$VM_HOSTNAME" ]]; then
  echo ""
  info "How will users reach this VM?"
  echo "  Enter a domain name (aurora.example.com) or press Enter for the detected IP."
  echo ""
  prompt VM_HOSTNAME "Hostname or IP" "${DETECTED_IP:-}"
fi

resolve_urls "$VM_HOSTNAME"

echo ""
ok "Frontend:  $FRONTEND_URL"
ok "API:       $BACKEND_URL_PUBLIC"
ok "WebSocket: $WEBSOCKET_URL"

# ─── LLM configuration ──────────────────────────────────────────────────────

echo ""
info "LLM provider configuration"

if [[ -n "${LLM_API_KEY:-}" ]]; then
  LLM_KEY="$LLM_API_KEY"
  LLM_PROVIDER_INPUT="${LLM_PROVIDER:-openrouter}"
  ok "Using LLM config from environment"
else
  prompt LLM_PROVIDER_INPUT "Provider (openrouter, openai, anthropic, google)" "openrouter"
  prompt LLM_KEY "API key for $LLM_PROVIDER_INPUT"
fi

if [[ "$LLM_PROVIDER_INPUT" == "openrouter" ]]; then
  LLM_PROVIDER_MODE="openrouter"
else
  LLM_PROVIDER_MODE="direct"
fi

# Build mode prompt (standard only)
if [[ "$PROFILE" == "standard" && "$NON_INTERACTIVE" != "true" ]]; then
  echo ""
  info "Image source:"
  echo "  [1] Build from source (recommended, latest code)"
  echo "  [2] Pull prebuilt images from GHCR (faster)"
  prompt BUILD_CHOICE "Enter 1 or 2" "$([[ "$BUILD_MODE" == "prebuilt" ]] && echo 2 || echo 1)"
  case "$BUILD_CHOICE" in
    2|prebuilt) BUILD_MODE="prebuilt" ;;
    *)          BUILD_MODE="build" ;;
  esac
fi

# ─── Validate non-interactive ────────────────────────────────────────────────

if [[ "$NON_INTERACTIVE" == "true" ]]; then
  _missing=()
  [[ -z "${LLM_KEY:-}" ]]     && _missing+=("LLM_API_KEY")
  [[ -z "${VM_HOSTNAME:-}" ]] && _missing+=("hostname (use --hostname)")
  if [[ ${#_missing[@]} -gt 0 ]]; then
    err "Non-interactive mode requires: ${_missing[*]}"
    exit 1
  fi
fi

# ─── Summary + confirm ──────────────────────────────────────────────────────

echo ""
echo "======================================================="
info "Deployment Summary"
echo "======================================================="
echo ""
echo "  Profile:      $PROFILE"
echo "  Hostname:     $VM_HOSTNAME"
echo "  Frontend:     $FRONTEND_URL"
echo "  API:          $BACKEND_URL_PUBLIC"
echo "  WebSocket:    $WEBSOCKET_URL"
echo "  LLM Provider: $LLM_PROVIDER_INPUT"
if [[ "$PROFILE" == "standard" ]]; then
  echo "  Build Mode:   $BUILD_MODE"
else
  echo "  Bundle:       $AIRTIGHT_BUNDLE"
fi
echo ""

confirm "Proceed with deployment?" || { info "Cancelled."; exit 0; }

# ─── Generate .env ───────────────────────────────────────────────────────────

echo ""
generate_env "$REPO_ROOT"

# ─── Build / pull / load images ─────────────────────────────────────────────

echo ""
if [[ "$PROFILE" == "airtight" ]]; then
  info "Loading images from bundle (this may take a few minutes)..."
  docker load < "$AIRTIGHT_BUNDLE"
  ok "Images loaded from bundle"
elif [[ "$BUILD_MODE" == "prebuilt" ]]; then
  info "Pulling prebuilt images from GHCR (tag: $VERSION)..."
  docker pull ghcr.io/arvo-ai/aurora-server:$VERSION
  docker pull ghcr.io/arvo-ai/aurora-frontend:$VERSION
  docker tag ghcr.io/arvo-ai/aurora-server:$VERSION aurora_server:latest
  docker tag ghcr.io/arvo-ai/aurora-server:$VERSION aurora_celery-worker:latest
  docker tag ghcr.io/arvo-ai/aurora-server:$VERSION aurora_celery-beat:latest
  docker tag ghcr.io/arvo-ai/aurora-server:$VERSION aurora_chatbot:latest
  docker tag ghcr.io/arvo-ai/aurora-frontend:$VERSION aurora_frontend:latest
  ok "Prebuilt images ready"
else
  info "Building images from source (this may take several minutes)..."
  docker compose -f "$COMPOSE_FILE" build
  ok "Images built"
fi

# ─── Start the stack ─────────────────────────────────────────────────────────

echo ""
info "Starting Aurora..."
docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
docker network rm aurora_default 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" up -d

info "Waiting for containers to start (~60-90s on first run)..."
_wait_timeout=180
_wait_elapsed=0
while [[ $_wait_elapsed -lt $_wait_timeout ]]; do
  if docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "aurora-server.*running" && \
     docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "frontend.*running"; then
    break
  fi
  sleep 5
  _wait_elapsed=$((_wait_elapsed + 5))
  printf "\r  Waiting... %ds / %ds" "$_wait_elapsed" "$_wait_timeout"
done
echo ""

# ─── Auto Vault ──────────────────────────────────────────────────────────────

echo ""
auto_vault "$COMPOSE_FILE" "$REPO_ROOT" || true

# ─── Health check ────────────────────────────────────────────────────────────

echo ""
health_gate "$COMPOSE_FILE" || true

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo "======================================================="
ok "Aurora deployment complete!"
echo "======================================================="
echo ""
echo "  Frontend:  $FRONTEND_URL"
echo "  API:       $BACKEND_URL_PUBLIC/health/"
echo "  WebSocket: $WEBSOCKET_URL"
echo ""
info "Useful commands:"
echo "  View logs:   cd $REPO_ROOT && docker compose -f $COMPOSE_FILE logs --tail 50 -f"
echo "  Stop:        cd $REPO_ROOT && make down"
echo "  Restart:     cd $REPO_ROOT && docker compose -f $COMPOSE_FILE restart"
echo ""

if [[ "$IS_IP" == "false" ]]; then
  warn "Ensure DNS for $VM_HOSTNAME points to ${DETECTED_IP:-this server}."
fi

if [[ -n "${DETECTED_IP:-}" ]] && [[ "$IS_IP" == "true" ]]; then
  info "Ensure your cloud security group allows inbound TCP on ports: 3000, 5080, 5006"
fi

echo ""
ok "Aurora is ready!"
