#!/usr/bin/env bash
set -euo pipefail

# Push all Aurora images to a private container registry, then update
# values.generated.yaml with the registry URL.
#
# Two modes (auto-detected unless --tarball is given):
#   1. Registry-to-registry  – pulls from GHCR / upstream, pushes to your registry.
#      No tarball, no extra disk. Requires internet access.
#   2. Tarball                – loads a pre-built airgap bundle, pushes from Docker cache.
#      For physically air-gapped environments with no internet.
#
# Usage:
#   ./deploy/push-to-registry.sh <registry>                           # auto-detect mode
#   ./deploy/push-to-registry.sh <registry> --tarball <path>          # force tarball mode
#
# Examples:
#   ./deploy/push-to-registry.sh registry.internal:5000
#   ./deploy/push-to-registry.sh harbor.corp/aurora --tarball ./aurora-airtight-v1.2.3-amd64.tar.gz

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/helm/aurora"
VALUES_FILE="$CHART_DIR/values.generated.yaml"

# ── Parse arguments ──────────────────────────────────────────────────────────

REGISTRY=""
TARBALL=""
FORCE_TARBALL=false

while [ $# -gt 0 ]; do
  case "$1" in
    --tarball)
      FORCE_TARBALL=true
      TARBALL="${2:-}"
      if [ -z "$TARBALL" ]; then
        echo "Error: --tarball requires a path argument"
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 <registry> [--tarball <path>]"
      echo ""
      echo "  registry        Target registry (e.g. registry.internal:5000)"
      echo "  --tarball PATH  Force tarball mode with a specific bundle"
      echo ""
      echo "Without --tarball, the script auto-detects the best method:"
      echo "  1. If GHCR/upstream registries are reachable → pull and push (no tarball needed)"
      echo "  2. If a local tarball exists → docker load and push"
      echo "  3. Otherwise → prints instructions to download the bundle"
      exit 0
      ;;
    *)
      if [ -z "$REGISTRY" ]; then
        REGISTRY="$1"
      else
        echo "Error: unexpected argument '$1'"
        echo "Usage: $0 <registry> [--tarball <path>]"
        exit 1
      fi
      shift
      ;;
  esac
done

if [ -z "$REGISTRY" ]; then
  echo "Usage: $0 <registry> [--tarball <path>]"
  echo "       $0 --help"
  exit 1
fi

REGISTRY="${REGISTRY%/}"

if ! command -v yq &>/dev/null; then
  echo "Error: yq is required but not installed."
  echo "Install: https://github.com/mikefarah/yq#install"
  exit 1
fi

# ── Image map ────────────────────────────────────────────────────────────────
# Format: upstream_source|private_registry_short_name
#
# upstream_source: full image reference for pulling from public registries
# private_registry_short_name: name used inside the private registry

declare -a IMAGE_MAP=(
  "ghcr.io/arvo-ai/aurora-server:latest|aurora-server:latest"
  "ghcr.io/arvo-ai/aurora-frontend:latest|aurora-frontend:latest"
  "postgres:15-alpine|postgres:15-alpine"
  "redis:7-alpine|redis:7-alpine"
  "hashicorp/vault:1.15|vault:1.15"
  "cr.weaviate.io/semitechnologies/weaviate:1.27.6|weaviate:1.27.6"
  "cr.weaviate.io/semitechnologies/transformers-inference:sentence-transformers-all-MiniLM-L6-v2|transformers-inference:sentence-transformers-all-MiniLM-L6-v2"
  "searxng/searxng:2025.5.8-7ca24eee4|searxng:2025.5.8-7ca24eee4"
  "minio/minio:RELEASE.2025-04-22T22-12-26Z|minio:RELEASE.2025-04-22T22-12-26Z"
  "minio/mc:RELEASE.2025-04-16T18-13-26Z|mc:RELEASE.2025-04-16T18-13-26Z"
  "registry.k8s.io/ingress-nginx/controller:v1.8.1|ingress-nginx-controller:v1.8.1"
  "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v20230407|ingress-nginx-kube-webhook-certgen:v20230407"
  "memgraph/memgraph-mage:3.8.1|memgraph-mage:3.8.1"
)

# Tarball-mode image names differ (docker save uses the local build names)
declare -a TARBALL_IMAGE_MAP=(
  "aurora_server:latest|aurora-server:latest"
  "aurora_frontend:latest|aurora-frontend:latest"
  "postgres:15-alpine|postgres:15-alpine"
  "redis:7-alpine|redis:7-alpine"
  "hashicorp/vault:1.15|vault:1.15"
  "cr.weaviate.io/semitechnologies/weaviate:1.27.6|weaviate:1.27.6"
  "cr.weaviate.io/semitechnologies/transformers-inference:sentence-transformers-all-MiniLM-L6-v2|transformers-inference:sentence-transformers-all-MiniLM-L6-v2"
  "searxng/searxng:2025.5.8-7ca24eee4|searxng:2025.5.8-7ca24eee4"
  "minio/minio:RELEASE.2025-04-22T22-12-26Z|minio:RELEASE.2025-04-22T22-12-26Z"
  "minio/mc:RELEASE.2025-04-16T18-13-26Z|mc:RELEASE.2025-04-16T18-13-26Z"
  "registry.k8s.io/ingress-nginx/controller:v1.8.1|ingress-nginx-controller:v1.8.1"
  "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v20230407|ingress-nginx-kube-webhook-certgen:v20230407"
  "memgraph/memgraph-mage:3.8.1|memgraph-mage:3.8.1"
)

TOTAL=${#IMAGE_MAP[@]}

# ── Determine mode ────────────────────────────────────────────────────────────

MODE=""

if [ "$FORCE_TARBALL" = true ]; then
  MODE="tarball"
  if [ ! -f "$TARBALL" ]; then
    echo "Error: tarball not found: $TARBALL"
    exit 1
  fi
else
  MODE="registry"
  # Verify upstream registries are reachable
  if ! curl -sS --connect-timeout 5 --max-time 10 -o /dev/null "https://ghcr.io/v2/" 2>/dev/null; then
    echo "============================================"
    echo "  Cannot reach upstream registries (ghcr.io)."
    echo "============================================"
    echo ""
    echo "  Registry-to-registry mode requires internet access."
    echo "  If your environment is air-gapped, re-run with a tarball:"
    echo ""
    echo "  See: https://docs.arvo.ai/deployment/kubernetes-airgap"
    exit 1
  fi
fi

# ── Banner ───────────────────────────────────────────────────────────────────

echo "============================================"
echo "  Aurora → Private Registry"
echo "  Registry: $REGISTRY"
if [ "$MODE" = "tarball" ]; then
  echo "  Mode:     tarball ($(du -h "$TARBALL" | cut -f1))"
  echo "  Tarball:  $TARBALL"
else
  echo "  Mode:     registry-to-registry (direct pull)"
fi
echo "============================================"
echo ""

# ── Mode: registry-to-registry ──────────────────────────────────────────────

push_registry_mode() {
  local FAILED=0

  if command -v skopeo &>/dev/null; then
    echo "Using skopeo (registry-to-registry, zero local disk)"
    echo ""
    local COUNT=0
    for mapping in "${IMAGE_MAP[@]}"; do
      local SRC="${mapping%%|*}"
      local DST="${mapping##*|}"
      COUNT=$((COUNT + 1))
      echo "  [${COUNT}/${TOTAL}] ${SRC} → ${REGISTRY}/${DST}"
      local ERR_MSG
      ERR_MSG=$(skopeo copy --all \
        "docker://${SRC}" \
        "docker://${REGISTRY}/${DST}" 2>&1) || {
        local LAST_LINE
        LAST_LINE=$(echo "$ERR_MSG" | tail -1)
        echo "    FAILED: ${LAST_LINE:-unknown error}"
        FAILED=$((FAILED + 1))
      }
    done
  elif command -v docker &>/dev/null && docker info &>/dev/null; then
    echo "Using docker (pull → tag → push)"
    echo "  Note: each image is pulled temporarily. ~3 GB free disk recommended."
    echo "  (skopeo avoids this — install it for zero-disk registry-to-registry copies)"

    DOCKER_FREE_KB=$(docker system df --format '{{.Reclaimable}}' 2>/dev/null | head -1 || true)
    DISK_FREE_KB=$(df -k "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" 2>/dev/null | awk 'NR==2{print $4}' || true)
    if [ -n "$DISK_FREE_KB" ] && [ "$DISK_FREE_KB" -lt 3145728 ] 2>/dev/null; then
      echo ""
      echo "  WARNING: Less than 3 GB free on Docker's disk."
      echo "  Free space: $(( DISK_FREE_KB / 1024 )) MB"
      echo "  Run 'docker system prune -a' to reclaim space, or install skopeo."
      echo ""
      printf "  Continue anyway? [y/N]: "
      read -r CONTINUE_LOW_DISK
      if [ "$CONTINUE_LOW_DISK" != "y" ] && [ "$CONTINUE_LOW_DISK" != "Y" ]; then
        exit 1
      fi
    fi

    echo ""
    local COUNT=0
    for mapping in "${IMAGE_MAP[@]}"; do
      local SRC="${mapping%%|*}"
      local DST="${mapping##*|}"
      COUNT=$((COUNT + 1))
      echo "  [${COUNT}/${TOTAL}] ${SRC} → ${REGISTRY}/${DST}"
      if docker pull "$SRC" 2>&1 | tail -1; then
        docker tag "$SRC" "${REGISTRY}/${DST}"
        if ! docker push "${REGISTRY}/${DST}" 2>&1 | tail -1; then
          echo "    FAILED: push error"
          FAILED=$((FAILED + 1))
        fi
        docker rmi "$SRC" "${REGISTRY}/${DST}" &>/dev/null || true
      else
        echo "    FAILED: pull error"
        FAILED=$((FAILED + 1))
      fi
    done
  else
    echo "Error: neither skopeo nor docker found."
    echo "Install one of:"
    echo "  skopeo: brew install skopeo  (macOS) | apt install skopeo  (Debian/Ubuntu)"
    echo "  Docker: https://docs.docker.com/get-docker/"
    exit 1
  fi

  echo ""
  return $FAILED
}

# ── Mode: tarball ────────────────────────────────────────────────────────────

push_tarball_mode() {
  local FAILED=0

  # Validate tarball integrity
  echo "Validating tarball..."
  if ! tar tzf "$TARBALL" >/dev/null 2>&1; then
    echo "Error: tarball is corrupt or truncated: $TARBALL"
    echo "  Size on disk: $(du -h "$TARBALL" | cut -f1)"
    echo "  A valid bundle is typically 8-12 GB."
    echo ""
    echo "  Re-download: ./deploy/download-bundle.sh"
    exit 1
  fi

  if ! command -v docker &>/dev/null || ! docker info &>/dev/null; then
    echo "Error: docker is required for tarball mode but is not available."
    echo "Install Docker: https://docs.docker.com/get-docker/"
    exit 1
  fi

  local TARBALL_SIZE
  TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
  local TARBALL_SIZE_KB
  TARBALL_SIZE_KB=$(du -k "$TARBALL" | cut -f1)
  local NEEDED_KB=$(( TARBALL_SIZE_KB * 2 ))

  local DISK_FREE_KB
  DISK_FREE_KB=$(df -k "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" 2>/dev/null | awk 'NR==2{print $4}' || true)

  echo "Loading images from tarball (${TARBALL_SIZE})..."
  echo "  Estimated space needed: ~$(( NEEDED_KB / 1048576 )) GB (tarball + Docker image cache)"

  if [ -n "$DISK_FREE_KB" ] && [ "$DISK_FREE_KB" -lt "$NEEDED_KB" ] 2>/dev/null; then
    echo ""
    echo "  WARNING: Not enough free disk space."
    echo "  Available: $(( DISK_FREE_KB / 1024 )) MB  |  Needed: ~$(( NEEDED_KB / 1024 )) MB"
    echo "  Run 'docker system prune -a' to reclaim space."
    echo ""
    printf "  Continue anyway? [y/N]: "
    read -r CONTINUE_LOW_DISK
    if [ "$CONTINUE_LOW_DISK" != "y" ] && [ "$CONTINUE_LOW_DISK" != "Y" ]; then
      exit 1
    fi
  fi

  echo ""
  if command -v pv &>/dev/null; then
    pv "$TARBALL" | docker load
  else
    echo "  (this may take a few minutes for large bundles)"
    docker load -i "$TARBALL"
  fi
  echo ""

  echo "Retagging and pushing ${TOTAL} images to ${REGISTRY}..."
  local PUSHED_TAGS=()
  local COUNT=0
  for mapping in "${TARBALL_IMAGE_MAP[@]}"; do
    local SRC="${mapping%%|*}"
    local DST="${mapping##*|}"
    COUNT=$((COUNT + 1))
    echo "  [${COUNT}/${TOTAL}] ${SRC} → ${REGISTRY}/${DST}"
    if docker image inspect "$SRC" &>/dev/null; then
      docker tag "$SRC" "${REGISTRY}/${DST}"
      if docker push "${REGISTRY}/${DST}" 2>&1 | tail -1; then
        PUSHED_TAGS+=("${REGISTRY}/${DST}" "$SRC")
      else
        echo "    FAILED"
        FAILED=$((FAILED + 1))
      fi
    else
      echo "    SKIP: not found in bundle"
    fi
  done
  echo ""

  echo "Cleaning up local images..."
  local CLEANED=0
  for img in "${PUSHED_TAGS[@]}"; do
    docker rmi "$img" &>/dev/null && CLEANED=$((CLEANED + 1))
  done
  docker image prune -f &>/dev/null || true
  echo "  Removed ${CLEANED} local image tags"
  echo ""

  return $FAILED
}

# ── Execute ──────────────────────────────────────────────────────────────────

FAILED=0
if [ "$MODE" = "registry" ]; then
  push_registry_mode || FAILED=$?
else
  push_tarball_mode || FAILED=$?
fi

# ── Update values.generated.yaml ─────────────────────────────────────────────

echo "Updating ${VALUES_FILE}..."
if [ ! -f "$VALUES_FILE" ]; then
  cp "$CHART_DIR/values.yaml" "$VALUES_FILE"
  echo "  Created values.generated.yaml from values.yaml"
fi
yq -i ".image.registry = \"${REGISTRY}\"" "$VALUES_FILE"
yq -i ".image.tag = \"latest\"" "$VALUES_FILE"
yq -i ".thirdPartyImages.registry = \"${REGISTRY}\"" "$VALUES_FILE"
echo "  Set image.registry = ${REGISTRY}"
echo "  Set image.tag = latest"
echo "  Set thirdPartyImages.registry = ${REGISTRY}"
echo ""

if [ "$FAILED" -gt 0 ]; then
  echo "WARNING: $FAILED image(s) failed to push. Check errors above."
  exit 1
fi

echo "Done! All ${TOTAL} images pushed to ${REGISTRY}."
