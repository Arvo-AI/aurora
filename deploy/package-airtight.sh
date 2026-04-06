#!/usr/bin/env bash
set -euo pipefail

# Package all Aurora Docker images into a single transferable tarball
# for deployment on airtight / restricted-egress VMs.
#
# Run this on a machine with internet access:
#   ./deploy/package-airtight.sh                          # default: linux/amd64
#   PLATFORM=linux/arm64 ./deploy/package-airtight.sh     # for ARM servers
#
# Transfer the resulting .tar.gz to the target VM, then:
#   make prod-airtight AIRTIGHT_BUNDLE=aurora-airtight-<version>.tar.gz

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION="${VERSION:-$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo "dev")}"
PLATFORM="${PLATFORM:-linux/amd64}"
ARCH="${PLATFORM#*/}"
OUTPUT="${REPO_ROOT}/aurora-airtight-${VERSION}-${ARCH}.tar.gz"

THIRD_PARTY_IMAGES=(
  "postgres:15-alpine"
  "redis:7-alpine"
  "hashicorp/vault:1.15"
  "busybox:1.37.0"
  "searxng/searxng:2025.5.8-7ca24eee4"
  "chrislusf/seaweedfs:4.07"
  "amazon/aws-cli:2.34.6"
  "memgraph/memgraph-mage:3.8.1"
  "memgraph/lab:3.8.0"
  "cr.weaviate.io/semitechnologies/weaviate:1.27.6"
  "cr.weaviate.io/semitechnologies/transformers-inference:sentence-transformers-all-MiniLM-L6-v2"
  "registry.k8s.io/ingress-nginx/controller:v1.8.1"
  "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v20230407"
)

AURORA_IMAGES=(
  "aurora_server:latest"
  "aurora_celery-worker:latest"
  "aurora_celery-beat:latest"
  "aurora_chatbot:latest"
  "aurora_frontend:latest"
)

echo "============================================"
echo "  Aurora Airtight Packager (${VERSION})"
echo "  Platform: ${PLATFORM}"
echo "============================================"
echo ""

# Step 1: Build Aurora images from source for the target platform
echo "[1/4] Building Aurora images from source (prod target, ${PLATFORM})..."
docker buildx build --platform "$PLATFORM" \
  -f "$REPO_ROOT/server/Dockerfile" --target prod \
  -t aurora_server:latest --load "$REPO_ROOT/server"

docker tag aurora_server:latest aurora_celery-worker:latest
docker tag aurora_server:latest aurora_celery-beat:latest
docker tag aurora_server:latest aurora_chatbot:latest

docker buildx build --platform "$PLATFORM" \
  -f "$REPO_ROOT/client/Dockerfile" --target prod \
  -t aurora_frontend:latest --load "$REPO_ROOT/client"
echo ""

# Step 2: Pull all third-party images for the target platform
echo "[2/4] Pulling third-party images (${PLATFORM})..."
for img in "${THIRD_PARTY_IMAGES[@]}"; do
  echo "  Pulling ${img}..."
  docker pull --platform "$PLATFORM" "$img"
done
echo ""

# Step 3: Save everything into a single tarball
echo "[3/4] Saving all images to ${OUTPUT}..."
ALL_IMAGES=("${AURORA_IMAGES[@]}" "${THIRD_PARTY_IMAGES[@]}")
docker save "${ALL_IMAGES[@]}" | gzip > "$OUTPUT"
echo ""

# Step 4: Generate checksum
echo "[4/4] Generating checksum..."
pushd "$(dirname "$OUTPUT")" > /dev/null
if command -v sha256sum &>/dev/null; then
  sha256sum "$(basename "$OUTPUT")" > "${OUTPUT}.sha256"
elif command -v shasum &>/dev/null; then
  shasum -a 256 "$(basename "$OUTPUT")" > "${OUTPUT}.sha256"
fi
popd > /dev/null
echo ""

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "============================================"
echo "  Bundle created: ${OUTPUT}"
echo "  Size: ${SIZE}"
echo "  Platform: ${PLATFORM}"
echo "  Checksum: ${OUTPUT}.sha256"
echo "============================================"
echo ""
echo "Transfer both files to the target VM, then run:"
echo "  make prod-airtight AIRTIGHT_BUNDLE=aurora-airtight-${VERSION}-${ARCH}.tar.gz"
