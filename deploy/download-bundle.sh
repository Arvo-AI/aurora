#!/usr/bin/env bash
set -euo pipefail

# Download the Aurora airtight bundle from Google Cloud Storage.
# Automatically resolves the latest release version if none is specified.
#
# Usage:
#   ./deploy/download-bundle.sh                    # latest release, auto-detect arch
#   ./deploy/download-bundle.sh v1.2.3             # specific version
#   ./deploy/download-bundle.sh v1.2.3 arm64       # specific version and arch
#   ./deploy/download-bundle.sh latest arm64        # latest release, specific arch

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
  VERSION=$(curl -fsSL --connect-timeout 10 "https://api.github.com/repos/arvo-ai/aurora/releases/latest" | grep '"tag_name"' | cut -d'"' -f4)
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

BUNDLE_URL="${BASE_URL}/${FILENAME}"
SHA_URL="${BASE_URL}/${FILENAME}.sha256"

# Pre-flight check: if the bundle doesn't exist yet the CI pipeline is
# probably still building it (typically 20-40 min after a tag push).
http_code=$(curl -s -o /dev/null -w "%{http_code}" --head "${BUNDLE_URL}" 2>/dev/null || true)
if [ "$http_code" != "200" ]; then
  echo ""
  echo "ERROR: ${FILENAME} is not available yet (HTTP $http_code)."
  echo "If this version was just tagged, the CI pipeline is likely still building the bundle."
  echo "Check progress at: https://github.com/arvo-ai/aurora/actions/workflows/publish-airtight.yml"
  echo "Retry this script once the build is complete."
  exit 1
fi

echo ""
echo "Downloading ${FILENAME}..."
curl -fSL -o "${FILENAME}" "${BUNDLE_URL}"
curl -fSL -o "${FILENAME}.sha256" "${SHA_URL}"

echo ""
echo "Verifying checksum..."
CHECKSUM_OK=false
if command -v sha256sum &>/dev/null; then
  sha256sum -c "${FILENAME}.sha256" && CHECKSUM_OK=true
elif command -v shasum &>/dev/null; then
  shasum -a 256 -c "${FILENAME}.sha256" && CHECKSUM_OK=true
else
  echo "WARNING: neither sha256sum nor shasum found, skipping verification."
  CHECKSUM_OK=true
fi

if [ "$CHECKSUM_OK" != true ]; then
  echo ""
  echo "ERROR: checksum verification failed."
  echo "  - The download may have been corrupted — try running this script again."
  echo "  - If the problem persists, the bundle file in GCS may be stale/incomplete."
  echo ""
  echo "Browse available bundles:"
  echo "  amd64: https://storage.googleapis.com/aurora-airtight-bucket/index.html"
  echo "  arm64: https://storage.googleapis.com/aurora-airtight-bucket-arm64/index.html"
  echo "Build status: https://github.com/arvo-ai/aurora/actions/workflows/publish-airtight.yml"
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
echo "  ./deploy/push-to-registry.sh <your-registry>"
