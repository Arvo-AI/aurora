#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Deployment — Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
# Single starting point for every Aurora deployment.
# Asks where you're deploying and hands off to the right script.
#
# Usage:
#   ./deploy/deploy.sh
#
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/lib/helpers.sh"

echo ""
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║         Aurora Deployment Wizard          ║"
echo "  ╚═══════════════════════════════════════════╝"
echo ""

# ── Choose deployment target ─────────────────────────────────────────────────

select_menu "What environment are you deploying Aurora on?" \
  "Personal computer — your laptop or desktop, for development or testing" \
  "VM or server      — a cloud VM, VPS, or bare-metal server" \
  "Kubernetes cluster — multi-node cluster, deployed with Helm"

TARGET="$MENU_RESULT"

case "$TARGET" in

# ═════════════════════════════════════════════════════════════════════════════
# 0 — Personal computer (Docker Compose)
# ═════════════════════════════════════════════════════════════════════════════
0)
  echo "  Setting up Aurora on your personal computer with Docker Compose."
  echo ""

  if ! check_tool docker; then
    warn "Docker is not installed."
    echo ""
    echo "  Install Docker Desktop: https://docs.docker.com/get-docker/"
    echo "  Then re-run this script."
    exit 1
  fi

  if ! docker info &>/dev/null 2>&1; then
    warn "Docker daemon is not running."
    echo "  Start Docker Desktop (or the Docker service) and re-run."
    exit 1
  fi

  ok "Docker is installed and running"

  cd "$REPO_ROOT"

  # Init if needed
  if [ ! -f .env ]; then
    info "First-time setup — generating .env and secrets..."
    make init
    echo ""
  fi

  # ── Start ──
  select_menu "How would you like to run Aurora?" \
    "Development      — hot-reload, build from source" \
    "Production test  — pull prebuilt images from GHCR" \
    "Production build — build production images from source"

  case "$MENU_RESULT" in
    0)
      info "Starting development stack..."
      make dev
      ;;
    1)
      info "Pulling prebuilt images and starting..."
      make prod-prebuilt
      ;;
    2)
      info "Building from source and starting..."
      make prod-local
      ;;
  esac
  ;;

# ═════════════════════════════════════════════════════════════════════════════
# 1 — VM or server (TODO: handled by separate workflow)
# ═════════════════════════════════════════════════════════════════════════════
1)
  echo ""
  echo "  Launching Aurora VM deployment..."
  echo ""
  if [ -f "$SCRIPT_DIR/vm-deploy.sh" ]; then
    bash "$SCRIPT_DIR/vm-deploy.sh"
  else
    warn "deploy/vm-deploy.sh not found."
    echo "  Download it from the Aurora repo or see the docs:"
    echo "  https://docs.arvo.ai/deployment/vm-deployment"
  fi
  ;;

# ═════════════════════════════════════════════════════════════════════════════
# 2 — Kubernetes cluster (Helm)
# ═════════════════════════════════════════════════════════════════════════════
2)
  echo "  Setting up Aurora on a Kubernetes cluster."
  echo ""

  select_menu "Does the cluster environment have internet access?" \
    "Yes — I can reach the internet from where I'll run the deployment" \
    "No  — the cluster is isolated / air-gapped"

  case "$MENU_RESULT" in
    0)
      # Connected — deploy directly, need registry
      REGISTRY=""
      if [ -t 0 ]; then
        printf "  Enter your container registry URL (e.g. registry.internal:5000): "
        read -r REGISTRY
        REGISTRY="${REGISTRY%/}"
      fi

      if [ -z "$REGISTRY" ]; then
        warn "A private container registry URL is required for Kubernetes deployments."
        echo "  Re-run and provide your registry, or run directly:"
        echo ""
        echo "    ./deploy/deploy-k8s-airgap.sh <registry-url>"
        exit 1
      fi

      EXTRA_ARGS=""
      printf "  Aurora version (leave blank for latest): "
      read -r VERSION_INPUT
      if [ -n "$VERSION_INPUT" ]; then
        EXTRA_ARGS="$VERSION_INPUT"
      fi

      echo ""
      bash "$SCRIPT_DIR/deploy-k8s-airgap.sh" "$REGISTRY" --mode standard $EXTRA_ARGS
      ;;

    1)
      # Isolated — figure out where they are
      echo ""
      select_menu "Where are you right now?" \
        "On a machine with internet — I need to download the bundle first" \
        "On the bastion / jump host — I already have the bundle here"

      case "$MENU_RESULT" in
        0)
          # On a connected machine — download bundle
          info "Downloading Aurora bundle for air-gapped transfer..."
          echo ""

          VERSION_INPUT=""
          printf "  Aurora version (leave blank for latest): "
          read -r VERSION_INPUT

          TARGET_VERSION="${VERSION_INPUT:-latest}"
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
          echo "  1. Transfer these files to your bastion / jump host:"
          echo ""
          ls -1 aurora-airtight-*.tar.gz aurora-*.tar.gz 2>/dev/null | grep -v "\.sha256" | sed 's/^/     /' || echo "     (check current directory for aurora-*.tar.gz files)"
          echo ""
          echo "     Example:  scp aurora-*.tar.gz bastion:/tmp/"
          echo ""
          echo "  2. On the bastion, extract the source and run:"
          echo ""
          echo "     tar xzf aurora-*.tar.gz   # the smaller one — source archive"
          echo "     cd aurora-*/"
          echo "     ./deploy/deploy.sh"
          echo "     # Choose 'Kubernetes cluster' → 'No' → 'On the bastion'"
          echo ""
          ;;

        1)
          # On the bastion — need registry, deploy with tarball
          REGISTRY=""
          if [ -t 0 ]; then
            printf "  Enter your container registry URL (e.g. registry.internal:5000): "
            read -r REGISTRY
            REGISTRY="${REGISTRY%/}"
          fi

          if [ -z "$REGISTRY" ]; then
            warn "A private container registry URL is required for Kubernetes deployments."
            echo "  Re-run and provide your registry, or run directly:"
            echo ""
            echo "    ./deploy/deploy-k8s-airgap.sh <registry-url>"
            exit 1
          fi

          EXTRA_ARGS=""
          printf "  Aurora version (leave blank for latest): "
          read -r VERSION_INPUT
          if [ -n "$VERSION_INPUT" ]; then
            EXTRA_ARGS="$VERSION_INPUT"
          fi

          echo ""
          bash "$SCRIPT_DIR/deploy-k8s-airgap.sh" "$REGISTRY" --mode bastion $EXTRA_ARGS
          ;;
      esac
      ;;
  esac
  ;;

esac
