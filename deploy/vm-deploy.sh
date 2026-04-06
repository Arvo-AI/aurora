#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora VM Deployment Script
# ─────────────────────────────────────────────────────────────────────────────
# One-command deployment for a fresh VM (Ubuntu/Debian, RHEL/Fedora, Amazon Linux).
# Installs Docker if needed, configures .env, generates secrets, and starts
# the full Aurora stack via Docker Compose.
#
# Usage:
#   ./deploy/vm-deploy.sh                       # interactive prompts
#   ./deploy/vm-deploy.sh --prebuilt            # pull prebuilt images from GHCR (default)
#   ./deploy/vm-deploy.sh --build               # build images from source
#   ./deploy/vm-deploy.sh --skip-docker         # skip Docker installation (already installed)
#   ./deploy/vm-deploy.sh --skip-firewall       # skip firewall rule setup
#   ./deploy/vm-deploy.sh --hostname aurora.example.com  # set hostname non-interactively
#   ./deploy/vm-deploy.sh --non-interactive     # use defaults for everything (requires --hostname and env vars)
#
# Required env vars for --non-interactive:
#   LLM_API_KEY, LLM_PROVIDER (openrouter|openai|anthropic|google)
#
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source shared helpers (fall back to inline if lib not found)
if [[ -f "$SCRIPT_DIR/lib/common.sh" ]]; then
  source "$SCRIPT_DIR/lib/common.sh"
else
  # Inline fallback for standalone usage
  prompt() {
    local var="$1" msg="$2" default="${3:-}"
    if $NON_INTERACTIVE; then
      printf -v "$var" '%s' "$default"
      return
    fi
    if [[ -n "$default" ]]; then
      read -rp "$msg [$default]: " val
      printf -v "$var" '%s' "${val:-$default}"
    else
      read -rp "$msg: " val
      while [[ -z "$val" ]]; do read -rp "$msg (required): " val; done
      printf -v "$var" '%s' "$val"
    fi
  }
  info()  { echo -e "\033[1;34m->\033[0m $1"; }
  ok()    { echo -e "\033[1;32m[ok]\033[0m $1"; }
  warn()  { echo -e "\033[1;33m[!]\033[0m $1"; }
  err()   { echo -e "\033[1;31m[x]\033[0m $1"; }
  detect_ip() {
    local ip=""
    ip=$(curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
    [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 2 -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)
    [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 2 -H "Metadata: true" "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" 2>/dev/null || true)
    [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 3 https://ifconfig.me 2>/dev/null || true)
    [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 3 https://api.ipify.org 2>/dev/null || true)
    echo "$ip"
  }
  detect_os() {
    if [[ -f /etc/os-release ]]; then . /etc/os-release; echo "$ID"
    elif command -v lsb_release &>/dev/null; then lsb_release -si | tr '[:upper:]' '[:lower:]'
    else echo "unknown"; fi
  }
  generate_secret() {
    if command -v openssl &>/dev/null; then openssl rand -hex 32
    elif command -v python3 &>/dev/null; then python3 -c "import secrets; print(secrets.token_hex(32))"
    else cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 64 | head -n 1; fi
  }
fi

BUILD_MODE="prebuilt"
SKIP_DOCKER=false
SKIP_FIREWALL=false
NON_INTERACTIVE=false
VM_HOSTNAME=""
VERSION="${VERSION:-latest}"

for arg in "$@"; do
  case $arg in
    --prebuilt)       BUILD_MODE="prebuilt" ;;
    --build)          BUILD_MODE="build" ;;
    --skip-docker)    SKIP_DOCKER=true ;;
    --skip-firewall)  SKIP_FIREWALL=true ;;
    --non-interactive) NON_INTERACTIVE=true ;;
    --hostname=*)     VM_HOSTNAME="${arg#*=}" ;;
    --hostname)       shift_next=true ;;
    *)
      if [[ "${shift_next:-}" == "true" ]]; then
        VM_HOSTNAME="$arg"
        shift_next=false
      else
        echo "Unknown arg: $arg"; exit 1
      fi
      ;;
  esac
done

# ─── Helpers (loaded from lib/common.sh above, or inline fallback) ────────

# ─── Banner ──────────────────────────────────────────────────────────────────

if [[ -f "$REPO_ROOT/scripts/show-logo.sh" ]]; then
  bash "$REPO_ROOT/scripts/show-logo.sh" 2>/dev/null || true
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Aurora VM Deployment"
echo "═══════════════════════════════════════════════"
echo ""

# ─── Step 1: Install Docker ──────────────────────────────────────────────────

if $SKIP_DOCKER; then
  warn "Skipping Docker installation (--skip-docker)"
elif command -v docker &>/dev/null && docker compose version &>/dev/null; then
  ok "Docker and Docker Compose already installed"
  docker --version
  docker compose version
else
  info "Installing Docker..."
  OS_ID=$(detect_os)

  case "$OS_ID" in
    ubuntu|debian|pop|linuxmint)
      sudo apt-get update -qq
      sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/$OS_ID/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
      sudo chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS_ID $(lsb_release -cs) stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
      sudo apt-get update -qq
      sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    rhel|centos|fedora|rocky|almalinux|amzn)
      sudo yum install -y yum-utils
      if [[ "$OS_ID" == "fedora" ]]; then
        sudo dnf install -y dnf-plugins-core
        sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      elif [[ "$OS_ID" == "amzn" ]]; then
        sudo yum install -y docker
        sudo systemctl start docker
        sudo systemctl enable docker
        # Amazon Linux uses amazon-linux-extras or bundled docker, compose plugin may need manual install
        if ! docker compose version &>/dev/null; then
          COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep tag_name | cut -d'"' -f4)
          sudo mkdir -p /usr/local/lib/docker/cli-plugins
          sudo curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
            -o /usr/local/lib/docker/cli-plugins/docker-compose
          sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        fi
      else
        sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      fi
      ;;
    *)
      err "Unsupported OS: $OS_ID. Please install Docker manually:"
      echo "  https://docs.docker.com/engine/install/"
      exit 1
      ;;
  esac

  sudo systemctl start docker 2>/dev/null || true
  sudo systemctl enable docker 2>/dev/null || true

  # Add current user to docker group (takes effect on next login)
  if ! groups | grep -q docker; then
    sudo usermod -aG docker "$USER" 2>/dev/null || true
    warn "Added $USER to docker group. You may need to log out and back in, or run: newgrp docker"
  fi

  ok "Docker installed"
  docker --version
  docker compose version
fi

# ─── Step 2: Firewall ────────────────────────────────────────────────────────

if $SKIP_FIREWALL; then
  warn "Skipping firewall setup (--skip-firewall)"
else
  info "Configuring firewall rules..."
  PORTS=(80 443 3000 5080 5006)

  if command -v ufw &>/dev/null; then
    for port in "${PORTS[@]}"; do
      sudo ufw allow "$port/tcp" 2>/dev/null || true
    done
    ok "UFW rules added for ports: ${PORTS[*]}"
  elif command -v firewall-cmd &>/dev/null; then
    for port in "${PORTS[@]}"; do
      sudo firewall-cmd --permanent --add-port="$port/tcp" 2>/dev/null || true
    done
    sudo firewall-cmd --reload 2>/dev/null || true
    ok "firewalld rules added for ports: ${PORTS[*]}"
  else
    warn "No firewall manager detected (ufw/firewalld). Ensure ports ${PORTS[*]} are open."
    warn "If using a cloud provider, check your security group / network firewall rules."
  fi
fi

# ─── Step 3: Detect IP & hostname ────────────────────────────────────────────

echo ""
info "Detecting public IP address..."
DETECTED_IP=$(detect_ip)

if [[ -n "$DETECTED_IP" ]]; then
  ok "Detected public IP: $DETECTED_IP"
else
  warn "Could not auto-detect public IP."
fi

if [[ -z "$VM_HOSTNAME" ]]; then
  echo ""
  info "How will users reach this VM?"
  echo "  1. Domain name (e.g. aurora.example.com) — recommended"
  echo "  2. IP address (${DETECTED_IP:-<your-ip>})"
  echo ""
  if $NON_INTERACTIVE; then
    VM_HOSTNAME="${DETECTED_IP:-localhost}"
  else
    prompt HOSTNAME_CHOICE "Enter domain name or press Enter for IP" "${DETECTED_IP:-}"
    VM_HOSTNAME="$HOSTNAME_CHOICE"
  fi
fi

# Determine if hostname is an IP or a domain
if [[ "$VM_HOSTNAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  IS_IP=true
  FRONTEND_URL="http://${VM_HOSTNAME}:3000"
  BACKEND_URL_PUBLIC="http://${VM_HOSTNAME}:5080"
  WEBSOCKET_URL="ws://${VM_HOSTNAME}:5006"
else
  IS_IP=false
  FRONTEND_URL="http://${VM_HOSTNAME}"
  BACKEND_URL_PUBLIC="http://${VM_HOSTNAME}:5080"
  WEBSOCKET_URL="ws://${VM_HOSTNAME}:5006"
fi

ok "Frontend URL:  $FRONTEND_URL"
ok "API URL:       $BACKEND_URL_PUBLIC"
ok "WebSocket URL: $WEBSOCKET_URL"

# ─── Step 4: Gather LLM configuration ────────────────────────────────────────

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

# Validate required variables (catches silent empties from non-interactive prompt)
if $NON_INTERACTIVE; then
  _missing=()
  [[ -z "${LLM_KEY:-}" ]]            && _missing+=("LLM_API_KEY")
  [[ -z "${LLM_PROVIDER_INPUT:-}" ]] && _missing+=("LLM_PROVIDER")
  [[ -z "${VM_HOSTNAME:-}" ]]        && _missing+=("hostname (use --hostname)")
  if [[ ${#_missing[@]} -gt 0 ]]; then
    err "Non-interactive mode requires the following variables: ${_missing[*]}"
    err "Set them via environment or CLI flags before running with --non-interactive."
    exit 1
  fi
fi

# ─── Step 5: Generate .env ───────────────────────────────────────────────────

echo ""
info "Generating configuration..."
cd "$REPO_ROOT"

POSTGRES_PW=$(generate_secret)
FLASK_SECRET=$(generate_secret)
AUTH_SECRET=$(generate_secret)
SEARXNG_SECRET=$(generate_secret)
MEMGRAPH_PW=$(generate_secret | head -c 32)

if [[ -f .env ]]; then
  cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
  warn "Existing .env backed up"
fi

cp .env.example .env
ok "Created .env from template"

# Core settings
sed -i.bak "s|^AURORA_ENV=.*|AURORA_ENV=production|" .env

# Database
sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$POSTGRES_PW|" .env

# Secrets
sed -i.bak "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$FLASK_SECRET|" .env
sed -i.bak "s|^AUTH_SECRET=.*|AUTH_SECRET=$AUTH_SECRET|" .env
sed -i.bak "s|^SEARXNG_SECRET=.*|SEARXNG_SECRET=$SEARXNG_SECRET|" .env

# Memgraph
sed -i.bak "s|^MEMGRAPH_PASSWORD=.*|MEMGRAPH_PASSWORD=$MEMGRAPH_PW|" .env

# URLs
sed -i.bak "s|^FRONTEND_URL=.*|FRONTEND_URL=$FRONTEND_URL|" .env
sed -i.bak "s|^NEXT_PUBLIC_BACKEND_URL=.*|NEXT_PUBLIC_BACKEND_URL=$BACKEND_URL_PUBLIC|" .env
sed -i.bak "s|^NEXT_PUBLIC_WEBSOCKET_URL=.*|NEXT_PUBLIC_WEBSOCKET_URL=$WEBSOCKET_URL|" .env

# SearXNG base URL (public-facing)
if $IS_IP; then
  sed -i.bak "s|^SEARXNG_BASE_URL=.*|SEARXNG_BASE_URL=http://${VM_HOSTNAME}:8082|" .env
else
  sed -i.bak "s|^SEARXNG_BASE_URL=.*|SEARXNG_BASE_URL=http://${VM_HOSTNAME}:8082|" .env
fi

# LLM provider
sed -i.bak "s|^LLM_PROVIDER_MODE=.*|LLM_PROVIDER_MODE=$LLM_PROVIDER_MODE|" .env
case "$LLM_PROVIDER_INPUT" in
  openrouter) sed -i.bak "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$LLM_KEY|" .env ;;
  openai)     sed -i.bak "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$LLM_KEY|" .env ;;
  anthropic)  sed -i.bak "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$LLM_KEY|" .env ;;
  google)     sed -i.bak "s|^GOOGLE_AI_API_KEY=.*|GOOGLE_AI_API_KEY=$LLM_KEY|" .env ;;
esac

# Clean up sed backup files
rm -f .env.bak

ok "Configuration generated"

# ─── Step 6: Pull or build images ────────────────────────────────────────────

echo ""
if [[ "$BUILD_MODE" == "prebuilt" ]]; then
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
  docker compose -f docker-compose.prod-local.yml build
  ok "Images built from source"
fi

# ─── Step 7: Start the stack ─────────────────────────────────────────────────

echo ""
info "Starting Aurora..."
docker compose -f docker-compose.prod-local.yml down --remove-orphans 2>/dev/null || true
docker network rm aurora_default 2>/dev/null || true
docker compose -f docker-compose.prod-local.yml up -d

# Wait for services to become healthy
info "Waiting for services to start (this takes ~60-90 seconds on first run)..."
TIMEOUT=180
ELAPSED=0
while [[ $ELAPSED -lt $TIMEOUT ]]; do
  HEALTHY=$(docker compose -f docker-compose.prod-local.yml ps --format json 2>/dev/null \
    | grep -c '"running"' 2>/dev/null || echo "0")
  TOTAL=$(docker compose -f docker-compose.prod-local.yml ps --format json 2>/dev/null \
    | wc -l 2>/dev/null | tr -d ' ' || echo "0")

  if docker compose -f docker-compose.prod-local.yml ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -q "aurora-server running" && \
     docker compose -f docker-compose.prod-local.yml ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -q "aurora-frontend running"; then
    break
  fi

  sleep 5
  ELAPSED=$((ELAPSED + 5))
  printf "\r  Waiting... %ds / %ds" "$ELAPSED" "$TIMEOUT"
done
echo ""

# ─── Step 8: Verify ──────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════"
info "Deployment complete!"
echo "═══════════════════════════════════════════════"
echo ""
docker compose -f docker-compose.prod-local.yml ps
echo ""
echo "  Frontend:  $FRONTEND_URL"
echo "  API:       $BACKEND_URL_PUBLIC/health/"
echo "  WebSocket: $WEBSOCKET_URL"
echo ""
info "Useful commands:"
echo "  View logs:     cd $REPO_ROOT && make prod-local-logs"
echo "  Stop Aurora:   cd $REPO_ROOT && make down"
echo "  Restart:       cd $REPO_ROOT && docker compose -f docker-compose.prod-local.yml restart"
echo ""

if ! $IS_IP; then
  warn "Make sure DNS for $VM_HOSTNAME points to ${DETECTED_IP:-this server}."
fi

if [[ -n "${DETECTED_IP:-}" ]] && $IS_IP; then
  info "If connecting from outside the VM, ensure your cloud security group allows inbound TCP on ports: 3000, 5080, 5006"
fi

echo ""
ok "Aurora is ready!"
