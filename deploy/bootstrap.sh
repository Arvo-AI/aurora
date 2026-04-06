#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Bootstrap
# ─────────────────────────────────────────────────────────────────────────────
# Zero-dependency entry point. Installs prerequisites, clones the repo, and
# launches the deployment wizard. Works on a completely fresh machine.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/bootstrap.sh | bash
#   wget -qO- https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/bootstrap.sh | bash
#
# The wizard will ask where you're deploying (laptop, VM, or Kubernetes)
# and guide you through the rest.
# ─────────────────────────────────────────────────────────────────────────────

# When piped (curl|bash), stdin is the script itself. Re-run from a temp file
# so the wizard inherits a clean stdin attached to the terminal.
if [[ ! -t 0 ]]; then
  _tmpscript=$(mktemp /tmp/aurora-bootstrap.XXXXXX)
  trap 'rm -f "$_tmpscript"' EXIT
  cat > "$_tmpscript"
  exec bash "$_tmpscript" "$@" < /dev/tty
fi

REPO_URL="https://github.com/arvo-ai/aurora.git"
INSTALL_DIR="${AURORA_INSTALL_DIR:-$HOME/aurora}"
BRANCH="${AURORA_BRANCH:-main}"

info()  { echo -e "\033[1;34m->\033[0m $1"; }
ok()    { echo -e "\033[1;32m[ok]\033[0m $1"; }
warn()  { echo -e "\033[1;33m[!]\033[0m $1"; }
err()   { echo -e "\033[1;31m[x]\033[0m $1"; }

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

install_pkg() {
  local os_id
  os_id=$(detect_os)
  case "$os_id" in
    ubuntu|debian|pop|linuxmint)
      sudo apt-get update -qq
      sudo apt-get install -y -qq "$@"
      ;;
    rhel|centos|fedora|rocky|almalinux|amzn)
      sudo yum install -y "$@"
      ;;
    *)
      err "Cannot auto-install on $os_id. Please install manually: $*"
      exit 1
      ;;
  esac
}

# ─── Install prerequisites ───────────────────────────────────────────────────

# git is required to clone the repo — can't continue without it
if ! command -v git &>/dev/null; then
  echo ""
  warn "git is not installed. It's required to download the Aurora source code."
  printf "  Install git? [Y/n]: "
  read -r _ans
  if [[ "${_ans:-Y}" =~ ^[Nn] ]]; then
    err "Cannot continue without git."
    echo "  Install it manually and re-run this script."
    exit 1
  fi
  install_pkg git
  ok "git installed"
fi

# These are used by various deploy paths but not all are needed immediately
optional_missing=()
for cmd in make jq; do
  command -v "$cmd" &>/dev/null || optional_missing+=("$cmd")
done

if [[ ${#optional_missing[@]} -gt 0 ]]; then
  echo ""
  info "The following tools are recommended but not installed: ${optional_missing[*]}"
  echo ""
  for cmd in "${optional_missing[@]}"; do
    case "$cmd" in
      make) echo "  - make    — needed for the local development path (make dev, make init)" ;;
      jq)   echo "  - jq      — needed for Vault auto-setup and some Kubernetes deploy paths" ;;
    esac
  done
  echo ""
  printf "  Install them now? [Y/n]: "
  read -r _ans
  if [[ "${_ans:-Y}" =~ ^[Nn] ]]; then
    warn "Skipping. Some deploy paths may prompt you to install these later."
  else
    install_pkg "${optional_missing[@]}"
    ok "Installed: ${optional_missing[*]}"
  fi
fi

# ─── Clone repo ──────────────────────────────────────────────────────────────

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing repo at $INSTALL_DIR"
  cd "$INSTALL_DIR"
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH" 2>/dev/null || {
    warn "Local changes detected -- resetting to origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
  }
else
  info "Cloning Aurora to $INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

ok "Repository ready at $INSTALL_DIR"

# ─── Run wizard ──────────────────────────────────────────────────────────────

chmod +x deploy/deploy.sh
exec deploy/deploy.sh "$@"
