#!/usr/bin/env bash
# Verify Aurora container image signatures using Sigstore cosign.
# Usage: scripts/cosign-verify.sh IMAGE [IMAGE ...]
# Exits 0 if cosign is not installed (with a warning).
# Exits 1 if cosign is installed but any verification fails.
set -euo pipefail

COSIGN_IDENTITY="https://github.com/Arvo-AI/aurora/.*"
COSIGN_ISSUER="https://token.actions.githubusercontent.com"

if ! command -v cosign >/dev/null 2>&1; then
    echo "WARNING: cosign not installed, skipping signature verification."
    echo "Install cosign to verify image provenance: https://docs.sigstore.dev/cosign/system_config/installation/"
    exit 0
fi

if [ $# -eq 0 ]; then
    echo "Usage: $0 IMAGE [IMAGE ...]" >&2
    exit 1
fi

echo "Verifying image signatures..."
for image in "$@"; do
    echo "  Verifying $image"
    if ! cosign verify \
        --certificate-identity-regexp="$COSIGN_IDENTITY" \
        --certificate-oidc-issuer="$COSIGN_ISSUER" \
        "$image" 2>&1; then
        echo "ERROR: Signature verification failed for $image" >&2
        exit 1
    fi
done
echo "Image signatures verified."
