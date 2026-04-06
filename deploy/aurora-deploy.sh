#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Deployment Wizard
# ─────────────────────────────────────────────────────────────────────────────
# Interactive deployment with profile selection, preflight checks, automatic
# Vault token extraction, and post-deploy health verification.
#
# Profiles:
#   standard  -- Internet-connected VM. Installs prerequisites, Docker, pulls/builds images.
#   airtight  -- Restricted-egress VM. Requires Docker pre-installed, source + image
#                bundle transferred manually. No internet calls are made.
#
# Usage:
#   ./deploy/aurora-deploy.sh                            # interactive
#   ./deploy/aurora-deploy.sh --profile standard         # standard VM deploy
#   ./deploy/aurora-deploy.sh --profile airtight         # air-tight / offline deploy
#   ./deploy/aurora-deploy.sh --build                    # build from source (default)
#   ./deploy/aurora-deploy.sh --prebuilt                 # pull prebuilt images
#   ./deploy/aurora-deploy.sh --skip-docker              # skip Docker installation
#   ./deploy/aurora-deploy.sh --skip-firewall            # skip firewall setup
#   ./deploy/aurora-deploy.sh --skip-prereqs             # skip prerequisite checks (advanced)
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
SKIP_PREREQS=false

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
    --skip-prereqs)     SKIP_PREREQS=true ;;
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
    SELECT_DESCRIPTIONS=(
      "Internet-connected VM. Build from source or pull prebuilt images."
      "Restricted-egress VM. Load images from a pre-transferred bundle."
    )
    select_option PROFILE_CHOICE "Standard Production" "Air-Tight Enterprise"
    unset SELECT_DESCRIPTIONS
    echo ""
    case "$PROFILE_CHOICE" in
      0) PROFILE="standard" ;;
      1) PROFILE="airtight" ;;
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

if [[ "$SKIP_PREREQS" == "true" ]]; then
  warn "Skipping prerequisite checks (--skip-prereqs)"
elif [[ "$PROFILE" == "standard" ]]; then
  ensure_prerequisites
elif [[ "$PROFILE" == "airtight" ]]; then
  ensure_airtight_prerequisites
fi
preflight "$PROFILE"

# ─── Profile: Air-Tight bundle validation ────────────────────────────────────

COMPOSE_FILE="docker-compose.prod-local.yml"

if [[ "$PROFILE" == "airtight" ]]; then
  COMPOSE_FILE="docker-compose.airtight.yml"

  if [[ ! -f "${REPO_ROOT}/${COMPOSE_FILE}" ]]; then
    err "Compose file not found: ${REPO_ROOT}/${COMPOSE_FILE}"
    err "Ensure the Aurora source tree is complete (missing docker-compose.airtight.yml)."
    exit 1
  fi
  echo "  Requirements for air-tight mode:"
  echo "    - Docker + Docker Compose pre-installed"
  echo "    - Aurora source transferred as tarball"
  echo "    - Image bundle (.tar.gz) transferred to this VM"
  echo ""

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

ensure_docker_access

# ─── Firewall ────────────────────────────────────────────────────────────────

if [[ "$PROFILE" == "airtight" ]]; then
  info "Skipping firewall setup (air-tight mode -- manage firewall externally)"
elif [[ "$SKIP_FIREWALL" == "true" ]]; then
  warn "Skipping firewall setup (--skip-firewall)"
else
  configure_firewall
fi

# ─── Hostname / IP ───────────────────────────────────────────────────────────

echo ""
DETECTED_IP=""
if [[ "$PROFILE" == "standard" ]]; then
  info "Detecting public IP address..."
  DETECTED_IP=$(detect_ip)
  [[ -n "$DETECTED_IP" ]] && ok "Detected IP: $DETECTED_IP" || warn "Could not auto-detect public IP."
fi

if [[ -z "$VM_HOSTNAME" ]]; then
  echo ""
  info "How will users reach this VM?"
  echo "  Enter a domain name, public IP, or internal/VPN IP."
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
  _llm_providers=("openrouter" "openai" "anthropic" "google")
  info "LLM provider:"
  select_option _llm_idx "OpenRouter" "OpenAI" "Anthropic" "Google"
  LLM_PROVIDER_INPUT="${_llm_providers[$_llm_idx]}"
  echo ""
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
  SELECT_DEFAULT="$([[ "$BUILD_MODE" == "build" ]] && echo 1 || echo 0)"
  select_option BUILD_CHOICE "Pull prebuilt images (recommended, faster)" "Build from source"
  unset SELECT_DEFAULT
  echo ""
  case "$BUILD_CHOICE" in
    1) BUILD_MODE="build" ;;
    *) BUILD_MODE="prebuilt" ;;
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

  # Skip load if critical images already exist
  if docker image inspect aurora_server:latest &>/dev/null && \
     docker image inspect aurora_frontend:latest &>/dev/null; then
    ok "Images already loaded (skipping bundle)"
  else
    _bundle_size=$(du -h "$AIRTIGHT_BUNDLE" | cut -f1)

    # Pre-check disk space vs bundle size
    _bundle_kb=$(du -k "$AIRTIGHT_BUNDLE" | cut -f1)
    _free_kb=$(df -k "${REPO_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}')
    _free_kb="${_free_kb:-0}"
    _need_kb=$(( _bundle_kb * 3 ))
    if [[ "$_free_kb" -lt "$_need_kb" ]] 2>/dev/null; then
      _free_h=$(( _free_kb / 1024 / 1024 ))
      _need_h=$(( _need_kb / 1024 / 1024 ))
      err "Insufficient disk space for image loading."
      err "Available: ~${_free_h} GB, estimated need: ~${_need_h} GB (3x bundle size)."
      err "Free space with: docker system prune -af"
      exit 1
    fi

  info "Loading images from bundle ($_bundle_size -- this may take 5-15 min)..."
  _load_rc=0
  if command -v pigz &>/dev/null; then
    info "Using pigz for parallel decompression"
    if command -v pv &>/dev/null; then
      pv "$AIRTIGHT_BUNDLE" | pigz -dc | docker load >/dev/null || _load_rc=$?
    else
      _loaded_start=$(date +%s)
      pigz -dc "$AIRTIGHT_BUNDLE" | docker load >/dev/null &
      _load_pid=$!
      while kill -0 "$_load_pid" 2>/dev/null; do
        _loaded_imgs=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | wc -l | tr -d ' ')
        _elapsed=$(( $(date +%s) - _loaded_start ))
        printf "\r  Loading... %dm%02ds elapsed, %s images loaded" \
          $((_elapsed/60)) $((_elapsed%60)) "$_loaded_imgs"
        sleep 5
      done
      echo ""
      wait "$_load_pid" || _load_rc=$?
    fi
  elif command -v pv &>/dev/null; then
    pv "$AIRTIGHT_BUNDLE" | docker load >/dev/null || _load_rc=$?
  else
    info "Tip: install pigz for ~2x faster image loading"
    _loaded_start=$(date +%s)
    docker load -i "$AIRTIGHT_BUNDLE" >/dev/null &
    _load_pid=$!
    while kill -0 "$_load_pid" 2>/dev/null; do
      _loaded_imgs=$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | wc -l | tr -d ' ')
      _elapsed=$(( $(date +%s) - _loaded_start ))
      printf "\r  Loading... %dm%02ds elapsed, %s images loaded" \
        $((_elapsed/60)) $((_elapsed%60)) "$_loaded_imgs"
      sleep 5
    done
    echo ""
    wait "$_load_pid" || _load_rc=$?
  fi

  if [[ "$_load_rc" -ne 0 ]]; then
    err "docker load failed (exit code: $_load_rc)."
    err "Possible causes:"
    echo "  - Corrupted or truncated bundle (re-transfer the file)"
    echo "  - Insufficient disk space (run: df -h / docker system prune -af)"
    exit 1
  fi

  # Verify critical images were loaded
  _missing_imgs=()
  for img in aurora_server:latest aurora_frontend:latest; do
    if ! docker image inspect "$img" &>/dev/null; then
      _missing_imgs+=("$img")
    fi
  done
  if [[ ${#_missing_imgs[@]} -gt 0 ]]; then
    err "Expected images not found after loading bundle: ${_missing_imgs[*]}"
    err "The bundle may have been built for a different architecture or is incomplete."
    info "Loaded images:"
    docker images --format '  {{.Repository}}:{{.Tag}}' 2>/dev/null | head -20
    exit 1
  fi

  ok "Images loaded from bundle"
  fi

  # docker load may not preserve all tags — ensure service aliases exist
  docker tag aurora_server:latest aurora_celery-worker:latest 2>/dev/null || true
  docker tag aurora_server:latest aurora_celery-beat:latest 2>/dev/null || true
  docker tag aurora_server:latest aurora_chatbot:latest 2>/dev/null || true

elif [[ "$BUILD_MODE" == "prebuilt" ]]; then
  info "Pulling prebuilt images from GHCR (tag: $VERSION)..."
  docker pull "ghcr.io/arvo-ai/aurora-server:${VERSION}"
  docker pull "ghcr.io/arvo-ai/aurora-frontend:${VERSION}"
  docker tag "ghcr.io/arvo-ai/aurora-server:${VERSION}" aurora_server:latest
  docker tag "ghcr.io/arvo-ai/aurora-server:${VERSION}" aurora_celery-worker:latest
  docker tag "ghcr.io/arvo-ai/aurora-server:${VERSION}" aurora_celery-beat:latest
  docker tag "ghcr.io/arvo-ai/aurora-server:${VERSION}" aurora_chatbot:latest
  docker tag "ghcr.io/arvo-ai/aurora-frontend:${VERSION}" aurora_frontend:latest
  ok "Prebuilt images ready"
else
  info "Building images from source (this may take several minutes)..."
  docker compose -f "$COMPOSE_FILE" build
  ok "Images built"
fi

# ─── Start the stack ─────────────────────────────────────────────────────────

echo ""
info "Starting Aurora..."

# Memgraph and Weaviate need elevated vm.max_map_count
_cur_map_count=$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)
if [[ "$_cur_map_count" -lt 262144 ]]; then
  sudo sysctl -w vm.max_map_count=262144 >/dev/null 2>&1 || true
fi

docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
docker network rm aurora_default 2>/dev/null || true
docker compose -f "$COMPOSE_FILE" up -d || warn "Some containers reported errors during startup (will check below)"

info "Waiting for containers to start (~60-90s on first run)..."
_wait_timeout=180
_wait_elapsed=0
while [[ $_wait_elapsed -lt $_wait_timeout ]]; do
  if docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "aurora-server.*running" && \
     docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -q "frontend.*running"; then
    break
  fi

  # Detect containers that have crashed or are in a restart loop
  _exited=$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -E 'exited|dead' || true)
  _restarting=$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep 'restarting' || true)
  if [[ -n "$_exited" || -n "$_restarting" ]]; then
    _crash_names=""
    [[ -n "$_exited" ]] && _crash_names="$_exited"
    [[ -n "$_restarting" ]] && _crash_names="${_crash_names:+${_crash_names}
}${_restarting}"
    echo ""
    err "Containers in unhealthy state:"
    echo "$_crash_names" | while read -r _cname _cstate; do
      err "  ${_cname} (${_cstate})"
      echo "  --- Last 15 lines of ${_cname} ---"
      docker logs --tail 15 "$_cname" 2>&1 | sed 's/^/  /'
      echo ""
    done
    echo ""
    err "Troubleshooting:"
    echo "  - Check full logs: docker compose -f $COMPOSE_FILE logs <service>"
    echo "  - Verify .env values: grep -E 'POSTGRES|VAULT|FLASK' .env"
    echo "  - Restart: docker compose -f $COMPOSE_FILE down && docker compose -f $COMPOSE_FILE up -d"
    exit 1
  fi

  sleep 5
  _wait_elapsed=$((_wait_elapsed + 5))
  printf "\r  Waiting... %ds / %ds" "$_wait_elapsed" "$_wait_timeout"
done
echo ""

if [[ $_wait_elapsed -ge $_wait_timeout ]]; then
  warn "Containers did not reach 'running' within ${_wait_timeout}s."
  info "Service status:"
  docker compose -f "$COMPOSE_FILE" ps 2>/dev/null || true
  echo ""
  warn "The stack may still be starting. Check logs:"
  echo "  docker compose -f $COMPOSE_FILE logs --tail 50 -f"
fi

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
