#!/bin/sh
set -e

# FRONTEND_DEV_RUNTIME selects the Next.js dev server process (Docker dev only).
#   bun  - Bun + Turbopack (default)
#   node - Node + Webpack (lower memory use in long-running Docker dev)
runtime="${FRONTEND_DEV_RUNTIME:-bun}"

case "$runtime" in
  node)
    exec npm run dev:node
    ;;
  bun)
    exec bun run dev:bun
    ;;
  *)
    echo "FRONTEND_DEV_RUNTIME must be 'bun' or 'node' (got: $runtime)" >&2
    exit 1
    ;;
esac
