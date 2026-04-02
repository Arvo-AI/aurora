#!/usr/bin/env bash
set -euo pipefail

# Push all Aurora images from an airgap tarball to a private container registry,
# then update values.generated.yaml with the registry URL.
#
# Must be run from the repo root (after extracting the release archive).
#
# Usage:
#   ./scripts/push-to-registry.sh <registry-url>                          # auto-detect tarball
#   ./scripts/push-to-registry.sh <tarball> <registry-url>                # explicit tarball
#
# Example:
#   ./scripts/push-to-registry.sh registry.internal:5000
#   ./scripts/push-to-registry.sh /tmp/aurora-airtight-v1.2.3-amd64.tar.gz registry.internal:5000

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/deploy/helm/aurora"
VALUES_FILE="$CHART_DIR/values.generated.yaml"

# Resolve tarball and registry from args
if [ $# -ge 2 ]; then
  TARBALL="$1"
  REGISTRY="$2"
elif [ $# -eq 1 ]; then
  REGISTRY="$1"
  # Auto-detect tarball: check cwd, then parent, then repo root
  TARBALL=""
  for dir in "." ".." "$REPO_ROOT"; do
    matches=( "$dir"/aurora-airtight-*.tar.gz )
    if [ -f "${matches[0]:-}" ]; then
      if [ ${#matches[@]} -eq 1 ]; then
        TARBALL="${matches[0]}"
        break
      else
        echo "Multiple tarballs found in $dir:"
        printf "  %s\n" "${matches[@]}"
        echo "Specify which one: $0 <tarball> <registry-url>"
        exit 1
      fi
    fi
  done
  if [ -z "$TARBALL" ]; then
    echo "Error: no aurora-airtight-*.tar.gz found in current, parent, or repo root directory."
    echo "Usage: $0 <tarball> <registry-url>"
    exit 1
  fi
  echo "Auto-detected tarball: $TARBALL"
else
  echo "Usage: $0 <registry-url>"
  echo "       $0 <tarball> <registry-url>"
  echo ""
  echo "  registry-url  Target registry (e.g. registry.internal:5000, harbor.corp/aurora)"
  echo "  tarball       Path to aurora-airtight-*.tar.gz (auto-detected if omitted)"
  exit 1
fi

REGISTRY="${REGISTRY%/}"

if [ ! -f "$TARBALL" ]; then
  echo "Error: tarball not found: $TARBALL"
  exit 1
fi

if ! command -v yq &>/dev/null; then
  echo "Error: yq is required but not installed."
  echo "Install: https://github.com/mikefarah/yq#install"
  exit 1
fi

echo "============================================"
echo "  Aurora → Private Registry Pusher"
echo "  Tarball:  $TARBALL"
echo "  Registry: $REGISTRY"
echo "============================================"
echo ""

# Image map: source image name → short name for the private registry
declare -a IMAGE_MAP=(
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
  "registry.k8s.io/ingress-nginx/controller:v1.8.1|ingress-nginx/controller:v1.8.1"
  "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v20230407|ingress-nginx/kube-webhook-certgen:v20230407"
)

TOTAL=${#IMAGE_MAP[@]}
FAILED=0

if command -v skopeo &>/dev/null; then
  # Fast path: stream directly from tarball to registry (no docker daemon needed)
  TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
  echo "Using skopeo (streaming ${TARBALL_SIZE} directly to registry, no local disk needed)"
  echo ""
  echo "[1/2] Pushing images to ${REGISTRY}..."
  COUNT=0
  for mapping in "${IMAGE_MAP[@]}"; do
    SRC="${mapping%%|*}"
    DST="${mapping##*|}"
    COUNT=$((COUNT + 1))
    echo "  [${COUNT}/${TOTAL}] ${SRC} → ${REGISTRY}/${DST}"
    if ! skopeo copy --all \
      "docker-archive:${TARBALL}:${SRC}" \
      "docker://${REGISTRY}/${DST}" 2>/dev/null; then
      echo "    WARNING: failed (image may not be in this bundle)"
      FAILED=$((FAILED + 1))
    fi
  done
  echo ""

  echo "[2/2] Updating ${VALUES_FILE}..."
else
  # Fallback: docker load → tag → push (requires ~2x tarball size in Docker disk)
  TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
  echo "WARNING: skopeo not found. Falling back to docker load/tag/push."
  echo "  This requires ~2x the tarball size (${TARBALL_SIZE}) in Docker disk space."
  echo "  Install skopeo to push directly from the tarball with no extra disk usage:"
  echo "    brew install skopeo  (macOS) | apt install skopeo  (Debian/Ubuntu)"
  echo ""
  if [ -t 0 ]; then
    printf "Continue with docker? [y/N]: "
    read -r CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
      echo "Aborted. Install skopeo and re-run."
      exit 1
    fi
  fi
  echo ""
  echo "[1/3] Loading images from tarball (${TARBALL_SIZE}, needs ~2x in Docker disk)..."
  if command -v pv &>/dev/null; then
    pv "$TARBALL" | docker load
  else
    docker load -i "$TARBALL"
  fi
  echo ""

  echo "[2/3] Retagging and pushing images..."
  COUNT=0
  for mapping in "${IMAGE_MAP[@]}"; do
    SRC="${mapping%%|*}"
    DST="${mapping##*|}"
    COUNT=$((COUNT + 1))
    echo "  [${COUNT}/${TOTAL}] ${SRC} → ${REGISTRY}/${DST}"
    if docker image inspect "$SRC" &>/dev/null; then
      docker tag "$SRC" "${REGISTRY}/${DST}"
      if ! docker push "${REGISTRY}/${DST}"; then
        echo "    WARNING: push failed"
        FAILED=$((FAILED + 1))
      fi
    else
      echo "    SKIP: source image not found (may not be in this bundle)"
    fi
  done
  echo ""

  echo "[3/3] Updating ${VALUES_FILE}..."
fi

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

echo "Done! ${FAILED} push failures."
echo ""
echo "Next steps:"
echo "  1. Edit ${VALUES_FILE} to configure LLM keys, URLs, secrets, ingress"
echo "  2. helm upgrade --install aurora-oss ./deploy/helm/aurora \\"
echo "       -n aurora --create-namespace --reset-values \\"
echo "       -f ${VALUES_FILE}"
echo ""
if [ $FAILED -gt 0 ]; then
  echo "WARNING: $FAILED image(s) failed to push. Check errors above."
  exit 1
fi
