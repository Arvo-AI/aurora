#!/usr/bin/env bash
set -euo pipefail

# Lightweight patch bundle for airtight deployments.
# Ships only the Aurora-built images + updated compose file — skips the
# ~20GB of unchanged third-party images (weaviate, transformers, etc.).
#
# Usage (build machine with internet):
#   ./scripts/package-airtight-patch.sh
#
# Transfer the .tar.gz to the target VM, then:
#   tar xzf aurora-patch-<version>-<arch>.tar.gz
#   docker load -i aurora-images.tar
#   cp docker-compose.airtight.yml /path/to/aurora/
#   docker compose -f docker-compose.airtight.yml up -d

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION="${VERSION:-$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo "dev")}"
PLATFORM="${PLATFORM:-linux/amd64}"
ARCH="${PLATFORM#*/}"
WORKDIR=$(mktemp -d)
OUTPUT="${REPO_ROOT}/aurora-patch-${VERSION}-${ARCH}.tar.gz"

echo "============================================"
echo "  Aurora Airtight Patch (${VERSION})"
echo "  Platform: ${PLATFORM}"
echo "============================================"
echo ""

echo "[1/3] Building Aurora images (${PLATFORM})..."
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

echo "[2/3] Saving images + compose file..."
docker save \
  aurora_server:latest \
  aurora_celery-worker:latest \
  aurora_celery-beat:latest \
  aurora_chatbot:latest \
  aurora_frontend:latest \
  > "$WORKDIR/aurora-images.tar"

cp "$REPO_ROOT/docker-compose.airtight.yml" "$WORKDIR/"
echo ""

echo "[3/3] Compressing..."
tar czf "$OUTPUT" -C "$WORKDIR" aurora-images.tar docker-compose.airtight.yml
rm -rf "$WORKDIR"

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo ""
echo "============================================"
echo "  Patch bundle: ${OUTPUT}"
echo "  Size: ${SIZE}"
echo "============================================"
echo ""
echo "On the target VM:"
echo "  tar xzf aurora-patch-${VERSION}-${ARCH}.tar.gz"
echo "  docker load -i aurora-images.tar"
echo "  cp docker-compose.airtight.yml /path/to/aurora/"
echo "  cd /path/to/aurora && docker compose -f docker-compose.airtight.yml up -d"
