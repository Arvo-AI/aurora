#!/usr/bin/env bash
# Destroy the preview stack: containers, volumes and its env file.
# Your dev stack's volumes and .env are a different Compose project and are
# not touched. Collected evidence (preview-evidence/) is left in place.
#
# Usage:
#   scripts/preview/down.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Compose needs an env file to parse the compose file, even to tear down.
created_env=0
if [ ! -f "$PREVIEW_ENV_FILE" ]; then
  cp "$PREVIEW_ROOT/.env.example" "$PREVIEW_ENV_FILE"
  created_env=1
fi

info "Removing the preview stack and its volumes"
compose down -v --remove-orphans

# The env file holds secrets that only match the volumes just deleted.
rm -f "$PREVIEW_ENV_FILE"
if [ "$created_env" = "0" ]; then
  info "Removed $(basename "$PREVIEW_ENV_FILE")"
fi
