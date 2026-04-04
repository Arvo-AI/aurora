#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Bootstrap
# ─────────────────────────────────────────────────────────────────────────────
# Zero-dependency entry point. Installs prerequisites, clones the repo, and
# launches the deployment wizard. Works on a completely fresh VM.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/bootstrap.sh | bash
#   wget -qO- https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/bootstrap.sh | bash
#
# All arguments are forwarded to the deployment wizard:
#   curl -fsSL <url> | bash -s -- --profile standard --prebuilt
#   curl -fsSL <url> | bash -s -- --profile airtight --bundle ~/bundle.tar.gz
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

missing=()
for cmd in git make jq curl; do
  command -v "$cmd" &>/dev/null || missing+=("$cmd")
done

if [[ ${#missing[@]} -gt 0 ]]; then
  info "Installing prerequisites: ${missing[*]}"
  install_pkg "${missing[@]}"
  ok "Prerequisites installed"
fi

# ─── Clone repo ──────────────────────────────────────────────────────────────

if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Updating existing repo at $INSTALL_DIR"
  cd "$INSTALL_DIR"
  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH" 2>/dev/null || git reset --hard "origin/$BRANCH"
else
  info "Cloning Aurora to $INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

ok "Repository ready at $INSTALL_DIR"

# ─── Run wizard ──────────────────────────────────────────────────────────────

chmod +x deploy/aurora-deploy.sh
exec deploy/aurora-deploy.sh "$@"
