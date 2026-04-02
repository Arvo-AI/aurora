#!/usr/bin/env bash
set -euo pipefail

# Download the Aurora airtight bundle from Google Cloud Storage.
# Automatically resolves the latest release version if none is specified.
#
# Usage:
#   ./scripts/download-bundle.sh                    # latest release, auto-detect arch
#   ./scripts/download-bundle.sh v1.2.3             # specific version
#   ./scripts/download-bundle.sh v1.2.3 arm64       # specific version and arch
#   ./scripts/download-bundle.sh latest arm64        # latest release, specific arch

VERSION="${1:-latest}"

if [ -n "${2:-}" ]; then
  ARCH="$2"
elif [ -t 0 ]; then
  printf "Cluster architecture — amd64 or arm64? [amd64]: "
  read -r ARCH
  ARCH="${ARCH:-amd64}"
else
  ARCH="amd64"
fi

if [ "$VERSION" = "latest" ]; then
  echo "Resolving latest release from GitHub..."
  VERSION=$(curl -fsSL "https://api.github.com/repos/arvo-ai/aurora/releases/latest" | grep '"tag_name"' | cut -d'"' -f4)
  if [ -z "$VERSION" ]; then
    echo "Error: could not resolve latest version from GitHub API."
    echo "Specify a version manually: $0 <version> [arch]"
    exit 1
  fi
  echo "Latest release: $VERSION"
fi

if [ "$ARCH" = "arm64" ]; then
  BUCKET="aurora-airtight-bucket-arm64"
else
  BUCKET="aurora-airtight-bucket"
fi
FILENAME="aurora-airtight-${VERSION}-${ARCH}.tar.gz"
BASE_URL="https://storage.googleapis.com/${BUCKET}"

echo ""
echo "Downloading ${FILENAME}..."
curl -LO "${BASE_URL}/${FILENAME}"
curl -LO "${BASE_URL}/${FILENAME}.sha256"

echo ""
echo "Verifying checksum..."
if sha256sum -c "${FILENAME}.sha256"; then
  echo "  Checksum OK"
else
  echo ""
  echo "ERROR: checksum verification failed. The file may be corrupted or the version may not exist."
  echo "Browse available bundles:"
  echo "  amd64: https://storage.googleapis.com/aurora-airtight-bucket/index.html"
  echo "  arm64: https://storage.googleapis.com/aurora-airtight-bucket-arm64/index.html"
  exit 1
fi

# Also download the source archive (Helm chart + scripts needed to deploy)
VERSION_STRIPPED="${VERSION#v}"
SOURCE_ARCHIVE="aurora-${VERSION_STRIPPED}.tar.gz"
echo ""
echo "Downloading source archive (Helm chart + scripts)..."
curl -fsSL -o "$SOURCE_ARCHIVE" "https://github.com/arvo-ai/aurora/archive/refs/tags/${VERSION}.tar.gz"
echo ""
echo "============================================"
echo "  Image bundle: ${FILENAME} ($(du -h "$FILENAME" | cut -f1))"
echo "  Source:       ${SOURCE_ARCHIVE}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  tar xzf ${SOURCE_ARCHIVE}"
echo "  cd aurora-${VERSION_STRIPPED}"
echo "  ./scripts/push-to-registry.sh <your-registry>"
