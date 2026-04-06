#!/usr/bin/env bash
# Shared helpers for Aurora deployment scripts.
# Source this file; do not execute directly.

[[ -n "${_AURORA_COMMON_LOADED:-}" ]] && return 0
_AURORA_COMMON_LOADED=1

# ─── Output ──────────────────────────────────────────────────────────────────

info()  { echo -e "\033[1;34m->\033[0m $1"; }
ok()    { echo -e "\033[1;32m[ok]\033[0m $1"; }
warn()  { echo -e "\033[1;33m[!]\033[0m $1"; }
err()   { echo -e "\033[1;31m[x]\033[0m $1"; }

# ─── Prompts ─────────────────────────────────────────────────────────────────

# Arrow-key menu selector.
# Usage: select_option RESULT_VAR "Option A" "Option B" ...
# Optional: set SELECT_DEFAULT=N (0-based) before calling to pre-select an option.
# Optional: set SELECT_DESCRIPTIONS=("desc A" "desc B") for subtitle text.
select_option() {
  local result_var="$1"; shift
  local options=("$@")
  local count=${#options[@]}
  local selected="${SELECT_DEFAULT:-0}"
  local descriptions=()
  [[ -n "${SELECT_DESCRIPTIONS+x}" ]] && descriptions=("${SELECT_DESCRIPTIONS[@]}")

  if [[ "${NON_INTERACTIVE:-false}" == "true" ]]; then
    printf -v "$result_var" '%s' "$selected"
    return
  fi

  local _total_lines=0

  _draw_menu() {
    local i
    _total_lines=0
    for ((i = 0; i < count; i++)); do
      if [[ $i -eq $selected ]]; then
        echo -e "  \033[1;36m> ${options[$i]}\033[0m" >&2
      else
        echo -e "    ${options[$i]}" >&2
      fi
      _total_lines=$((_total_lines + 1))
      if [[ ${#descriptions[@]} -gt 0 && -n "${descriptions[$i]:-}" ]]; then
        if [[ $i -eq $selected ]]; then
          echo -e "      \033[0;36m${descriptions[$i]}\033[0m" >&2
        else
          echo -e "      \033[2m${descriptions[$i]}\033[0m" >&2
        fi
        _total_lines=$((_total_lines + 1))
      fi
    done
  }

  _draw_menu
  while true; do
    IFS= read -rsn1 key < /dev/tty
    if [[ "$key" == $'\x1b' ]]; then
      read -rsn2 -t 0.1 key2 < /dev/tty
      key+="$key2"
    fi
    case "$key" in
      $'\x1b[A'|k) selected=$(( (selected - 1 + count) % count )) ;;
      $'\x1b[B'|j) selected=$(( (selected + 1) % count )) ;;
      "")          break ;;
      *)           continue ;;
    esac
    printf '\033[%dA' "$_total_lines" >&2
    _draw_menu
  done

  printf -v "$result_var" '%s' "$selected"
}

prompt() {
  local var="$1" msg="$2" default="${3:-}"
  if [[ "${NON_INTERACTIVE:-false}" == "true" ]]; then
    printf -v "$var" '%s' "$default"
    return
  fi
  if [[ -n "$default" ]]; then
    read -rp "$msg [$default]: " val < /dev/tty
    printf -v "$var" '%s' "${val:-$default}"
  else
    read -rp "$msg: " val < /dev/tty
    while [[ -z "$val" ]]; do read -rp "$msg (required): " val < /dev/tty; done
    printf -v "$var" '%s' "$val"
  fi
}

confirm() {
  local msg="${1:-Continue?}"
  if [[ "${NON_INTERACTIVE:-false}" == "true" ]]; then return 0; fi
  echo "$msg"
  select_option _confirm_choice "Yes" "No"
  echo ""
  [[ "$_confirm_choice" -eq 0 ]]
}

# ─── Compatibility shims (for scripts that used the old helpers.sh API) ──────

check_tool() { command -v "$1" &>/dev/null; }

# select_menu "Prompt" "Opt A" "Opt B" ... → sets MENU_RESULT (0-based index)
select_menu() {
  local _prompt="$1"; shift
  local _opts=("$@")
  echo "$_prompt"
  echo ""
  select_option MENU_RESULT "${_opts[@]}"
  echo ""
}

# prompt_default "Question" "default" → sets PROMPT_RESULT
prompt_default() {
  prompt PROMPT_RESULT "$1" "$2"
}

# ─── Detection ───────────────────────────────────────────────────────────────

detect_ip() {
  local ip=""
  ip=$(curl -sf --connect-timeout 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 2 -H "Metadata-Flavor: Google" \
    http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 2 -H "Metadata: true" \
    "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 3 https://ifconfig.me 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -sf --connect-timeout 3 https://api.ipify.org 2>/dev/null || true)
  echo "$ip"
}

detect_os() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    echo "$ID"
  elif command -v lsb_release &>/dev/null; then
    lsb_release -si | tr '[:upper:]' '[:lower:]'
  else
    echo "unknown"
  fi
}

generate_secret() {
  local secret=""
  if command -v openssl &>/dev/null; then
    secret=$(openssl rand -hex 32)
  elif command -v python3 &>/dev/null; then
    secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  else
    secret=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w 64 | head -n 1)
  fi
  if [[ -z "$secret" ]]; then
    err "Failed to generate secret. Ensure openssl or python3 is available."
    exit 1
  fi
  echo "$secret"
}

# Resolve hostname to URLs. Sets FRONTEND_URL, BACKEND_URL_PUBLIC, WEBSOCKET_URL, IS_IP.
resolve_urls() {
  local host="$1"
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    IS_IP=true
    FRONTEND_URL="http://${host}:3000"
  else
    IS_IP=false
    FRONTEND_URL="http://${host}"
  fi
  BACKEND_URL_PUBLIC="http://${host}:5080"
  WEBSOCKET_URL="ws://${host}:5006"
}

# ─── Preflight ───────────────────────────────────────────────────────────────

ensure_prerequisites() {
  local missing=()
  for cmd in git make jq curl; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  [[ ${#missing[@]} -eq 0 ]] && return 0

  info "Installing missing prerequisites: ${missing[*]}"
  local os_id
  os_id=$(detect_os)
  case "$os_id" in
    ubuntu|debian|pop|linuxmint)
      sudo apt-get update -qq
      sudo apt-get install -y -qq "${missing[@]}"
      ;;
    rhel|centos|fedora|rocky|almalinux|amzn)
      sudo yum install -y "${missing[@]}"
      ;;
    *)
      err "Could not auto-install ${missing[*]} on $os_id. Please install them manually."
      exit 1
      ;;
  esac
  ok "Prerequisites installed: ${missing[*]}"
}

DOCKER_INSTALL_DOCS="https://arvo-ai.github.io/aurora/docs/deployment/install-docker"

_pigz_hint() {
  if ! command -v pigz &>/dev/null; then
    info "Optional: install pigz for ~2x faster image loading"
    info "  Debian/Ubuntu: sudo apt-get install -y pigz"
    info "  RHEL/CentOS:   sudo yum install -y pigz"
  fi
}

# Validate all prerequisites for air-tight (offline) deployments.
# Collects every failure and prints a numbered remediation checklist.
ensure_airtight_prerequisites() {
  local issues=()
  local n=0

  # Docker binary
  if ! command -v docker &>/dev/null; then
    n=$((n + 1))
    issues+=("${n}. Docker is not installed."$'\n'"     See: ${DOCKER_INSTALL_DOCS}")
  else
    # Docker daemon running
    if ! docker info &>/dev/null 2>&1; then
      n=$((n + 1))
      issues+=("${n}. Docker daemon is not running."$'\n'"     Run: sudo systemctl start docker"$'\n'"     See: ${DOCKER_INSTALL_DOCS}")
    fi

    # Docker Compose v2
    if ! docker compose version &>/dev/null 2>&1; then
      n=$((n + 1))
      issues+=("${n}. Docker Compose v2 not found (need 'docker compose', not 'docker-compose')."$'\n'"     See: ${DOCKER_INSTALL_DOCS}")
    else
      local compose_ver
      compose_ver=$(docker compose version --short 2>/dev/null || echo "0")
      compose_ver="${compose_ver#v}"
      local compose_major="${compose_ver%%.*}"
      if [[ "$compose_major" -lt 2 ]] 2>/dev/null; then
        n=$((n + 1))
        issues+=("${n}. Docker Compose version too old: v${compose_ver} (need v2+)."$'\n'"     See: ${DOCKER_INSTALL_DOCS}")
      fi
    fi

    # Docker group membership (skip if running as root)
    if [[ "$(id -u)" -ne 0 ]] && ! docker ps &>/dev/null 2>&1; then
      n=$((n + 1))
      issues+=("${n}. Current user '${USER}' cannot run Docker without sudo."$'\n'"     Run: sudo usermod -aG docker ${USER} && newgrp docker")
    fi
  fi

  # Required CLI tools
  local -A tool_pkgs=(
    [jq]="jq"
    [sed]="sed"
    [grep]="grep"
    [tar]="tar"
  )
  for cmd in jq sed grep tar; do
    if ! command -v "$cmd" &>/dev/null; then
      n=$((n + 1))
      local pkg="${tool_pkgs[$cmd]}"
      issues+=("${n}. '${cmd}' is not installed."$'\n'"     Debian/Ubuntu:  sudo apt-get install -y ${pkg}"$'\n'"     RHEL/CentOS:    sudo yum install -y ${pkg}")
    fi
  done

  # Checksum tool (sha256sum or shasum)
  if ! command -v sha256sum &>/dev/null && ! command -v shasum &>/dev/null; then
    n=$((n + 1))
    issues+=("${n}. No checksum tool found (sha256sum or shasum)."$'\n'"     Debian/Ubuntu:  sudo apt-get install -y coreutils"$'\n'"     RHEL/CentOS:    sudo yum install -y coreutils")
  fi

  [[ ${#issues[@]} -eq 0 ]] && { ok "All air-tight prerequisites satisfied"; _pigz_hint; return 0; }

  echo ""
  err "Prerequisites check failed. Fix the following before re-running:"
  echo ""
  for issue in "${issues[@]}"; do
    echo "  $issue"
    echo ""
  done
  exit 1
}

preflight() {
  local profile="${1:-standard}"
  local failed=0
  info "Running preflight checks..."

  # CPU
  local cpus
  cpus=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 0)
  if [[ "$cpus" -ge 4 ]]; then
    ok "CPU: ${cpus} cores"
  else
    warn "CPU: ${cpus} cores (minimum 4 recommended)"
  fi

  # RAM
  local mem_kb mem_gb
  if [[ -f /proc/meminfo ]]; then
    mem_kb=$(awk '/^MemTotal/ {print $2}' /proc/meminfo)
  else
    mem_kb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 ))
  fi
  mem_gb=$(( mem_kb / 1024 / 1024 ))
  if [[ "$mem_gb" -ge 6 ]]; then
    ok "RAM: ${mem_gb} GB"
  else
    err "RAM: ${mem_gb} GB (minimum 6 GB required)"
    failed=1
  fi

  # Disk — airtight needs more headroom for the image bundle
  local free_gb disk_rec
  if [[ "$profile" == "airtight" ]]; then disk_rec=80; else disk_rec=40; fi
  free_gb=$(df -BG "${REPO_ROOT:-.}" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}')
  if [[ -z "$free_gb" ]]; then
    free_gb=$(df -g "${REPO_ROOT:-.}" 2>/dev/null | awk 'NR==2 {print $4}')
  fi
  free_gb="${free_gb:-0}"
  if [[ "$free_gb" -ge "$disk_rec" ]]; then
    ok "Disk: ${free_gb} GB free"
  elif [[ "$free_gb" -ge $(( disk_rec / 2 )) ]]; then
    warn "Disk: ${free_gb} GB free (${disk_rec} GB+ recommended)"
  else
    err "Disk: ${free_gb} GB free (${disk_rec} GB+ recommended, builds may fail)"
    failed=1
  fi

  # Docker
  if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    local docker_ver compose_ver
    docker_ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null || docker --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || echo "0")
    docker_ver="${docker_ver%%+*}"
    local docker_major="${docker_ver%%.*}"
    compose_ver=$(docker compose version --short 2>/dev/null || echo "0")
    compose_ver="${compose_ver#v}"
    local compose_major="${compose_ver%%.*}"

    local docker_ok=true
    if [[ "$docker_major" -lt 24 ]] 2>/dev/null; then
      err "Docker version too old: ${docker_ver} (need 24.0+)"
      err "See: ${DOCKER_INSTALL_DOCS}"
      docker_ok=false
      failed=1
    fi
    if [[ "$compose_major" -lt 2 ]] 2>/dev/null; then
      err "Docker Compose version too old: ${compose_ver} (need v2+)"
      err "See: ${DOCKER_INSTALL_DOCS}"
      docker_ok=false
      failed=1
    fi
    if [[ "$docker_ok" == "true" ]]; then
      ok "Docker: $(docker --version 2>/dev/null | head -c 60), Compose v${compose_ver}"
    fi
  elif [[ "$profile" == "airtight" ]]; then
    err "Docker not found. Air-tight mode requires Docker pre-installed."
    err "See: ${DOCKER_INSTALL_DOCS}"
    echo ""
    err "Aborting -- cannot proceed without Docker in air-tight mode."
    exit 1
  elif [[ "${SKIP_DOCKER:-false}" == "true" ]]; then
    err "Docker not found and --skip-docker is set"
    failed=1
  else
    info "Docker: not installed (will be installed)"
  fi

  if [[ "$failed" -eq 1 ]]; then
    echo ""
    if [[ "$profile" == "airtight" ]]; then
      err "Preflight failed. Fix the issues above before re-running."
      exit 1
    fi
    if ! confirm "Preflight issues detected. Continue anyway?"; then
      err "Aborting."
      exit 1
    fi
  fi
  echo ""
}

# ─── Docker install ──────────────────────────────────────────────────────────

install_docker() {
  if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    ok "Docker and Docker Compose already installed"
    return 0
  fi

  info "Installing Docker..."
  local os_id
  os_id=$(detect_os)

  case "$os_id" in
    ubuntu|debian|pop|linuxmint)
      sudo apt-get update -qq
      sudo apt-get install -y -qq ca-certificates curl gnupg lsb-release
      sudo install -m 0755 -d /etc/apt/keyrings
      curl -fsSL "https://download.docker.com/linux/$os_id/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
      sudo chmod a+r /etc/apt/keyrings/docker.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$os_id $(lsb_release -cs) stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
      sudo apt-get update -qq
      sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    rhel|centos|fedora|rocky|almalinux|amzn)
      sudo yum install -y yum-utils
      if [[ "$os_id" == "fedora" ]]; then
        sudo dnf install -y dnf-plugins-core
        sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
      elif [[ "$os_id" == "amzn" ]]; then
        sudo yum install -y docker
        sudo systemctl start docker
        sudo systemctl enable docker
        if ! docker compose version &>/dev/null; then
          local cv
          cv=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep tag_name | cut -d'"' -f4)
          sudo mkdir -p /usr/local/lib/docker/cli-plugins
          sudo curl -SL "https://github.com/docker/compose/releases/download/${cv}/docker-compose-$(uname -s)-$(uname -m)" \
            -o /usr/local/lib/docker/cli-plugins/docker-compose
          sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
        fi
      else
        sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      fi
      ;;
    *)
      err "Unsupported OS: $os_id. Install Docker manually: https://docs.docker.com/engine/install/"
      exit 1
      ;;
  esac

  sudo systemctl start docker 2>/dev/null || true
  sudo systemctl enable docker 2>/dev/null || true

  if ! groups | grep -q docker; then
    sudo usermod -aG docker "$USER" 2>/dev/null || true
  fi

  ok "Docker installed"
}

# After Docker install, the current session may not have the docker group yet.
# Define a wrapper so all docker calls in this script transparently use sudo.
ensure_docker_access() {
  if docker ps &>/dev/null 2>&1; then
    return 0
  fi
  if sudo docker ps &>/dev/null 2>&1; then
    info "Using sudo for Docker (group membership takes effect on next login)"
    docker() { command sudo docker "$@"; }
    return 0
  fi
  err "Cannot connect to Docker daemon (even with sudo)."
  err "Check that Docker installed correctly: sudo systemctl status docker"
  exit 1
}

# ─── Firewall ────────────────────────────────────────────────────────────────

configure_firewall() {
  info "Configuring firewall rules..."
  local ports=(80 443 3000 5080 5006)

  if command -v ufw &>/dev/null; then
    for port in "${ports[@]}"; do
      sudo ufw allow "$port/tcp" 2>/dev/null || true
    done
    ok "UFW rules added for ports: ${ports[*]}"
  elif command -v firewall-cmd &>/dev/null; then
    for port in "${ports[@]}"; do
      sudo firewall-cmd --permanent --add-port="$port/tcp" 2>/dev/null || true
    done
    sudo firewall-cmd --reload 2>/dev/null || true
    ok "firewalld rules added for ports: ${ports[*]}"
  else
    warn "No firewall manager detected. Ensure ports ${ports[*]} are open in your cloud security group."
  fi
}

# ─── .env generation ─────────────────────────────────────────────────────────

# Writes a key=value into .env, replacing existing line or appending.
env_set() {
  local key="$1" value="$2" file="${3:-.env}"
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

generate_env() {
  local repo_root="$1"
  cd "$repo_root"

  if [[ ! -f .env.example ]]; then
    err ".env.example not found in $repo_root"
    err "Ensure the Aurora source tree is complete."
    exit 1
  fi

  # Preserve secrets from existing .env so re-runs don't break stateful services
  local _prev_postgres="" _prev_flask="" _prev_auth="" _prev_searxng="" _prev_memgraph=""
  if [[ -f .env ]]; then
    _prev_postgres=$(sed -n 's/^POSTGRES_PASSWORD=//p' .env 2>/dev/null || true)
    _prev_flask=$(sed -n 's/^FLASK_SECRET_KEY=//p' .env 2>/dev/null || true)
    _prev_auth=$(sed -n 's/^AUTH_SECRET=//p' .env 2>/dev/null || true)
    _prev_searxng=$(sed -n 's/^SEARXNG_SECRET=//p' .env 2>/dev/null || true)
    _prev_memgraph=$(sed -n 's/^MEMGRAPH_PASSWORD=//p' .env 2>/dev/null || true)
    cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
    warn "Existing .env backed up (secrets preserved)"
  fi

  cp .env.example .env
  ok "Created .env from template"

  env_set AURORA_ENV production
  env_set POSTGRES_PASSWORD "${_prev_postgres:-$(generate_secret)}"
  env_set FLASK_SECRET_KEY "${_prev_flask:-$(generate_secret)}"
  env_set AUTH_SECRET "${_prev_auth:-$(generate_secret)}"
  env_set SEARXNG_SECRET "${_prev_searxng:-$(generate_secret)}"
  env_set MEMGRAPH_PASSWORD "${_prev_memgraph:-$(generate_secret | head -c 32)}"
  env_set FRONTEND_URL "$FRONTEND_URL"
  env_set NEXT_PUBLIC_BACKEND_URL "$BACKEND_URL_PUBLIC"
  env_set NEXT_PUBLIC_WEBSOCKET_URL "$WEBSOCKET_URL"
  env_set SEARXNG_BASE_URL "http://${VM_HOSTNAME}:8082"
  env_set LLM_PROVIDER_MODE "$LLM_PROVIDER_MODE"

  case "$LLM_PROVIDER_INPUT" in
    openrouter) env_set OPENROUTER_API_KEY "$LLM_KEY" ;;
    openai)     env_set OPENAI_API_KEY "$LLM_KEY" ;;
    anthropic)  env_set ANTHROPIC_API_KEY "$LLM_KEY" ;;
    google)     env_set GOOGLE_AI_API_KEY "$LLM_KEY" ;;
  esac

  rm -f .env.bak

  if ! grep -q "^POSTGRES_PASSWORD=" .env 2>/dev/null; then
    err ".env generation failed -- critical keys missing."
    err "Check disk space and file permissions in $repo_root"
    exit 1
  fi
  ok "Configuration generated"
}
