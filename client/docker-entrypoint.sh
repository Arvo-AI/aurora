#!/bin/sh
set -e

# Generate /app/public/env-config.js from runtime environment variables.
# This lets a single prebuilt image work on any host without rebuilding.

# Escape a value for safe embedding inside a JS string literal.
# Handles: backslashes, double quotes, newlines, and </script> injection.
sanitize() {
  printf '%s' "$1" \
    | sed -e 's/\\/\\\\/g' \
          -e 's/"/\\"/g' \
          -e 's/</\\u003c/g' \
    | tr -d '\n\r'
}

cat > /app/public/env-config.js <<JSEOF
window.__ENV = {
  NEXT_PUBLIC_BACKEND_URL: "$(sanitize "${NEXT_PUBLIC_BACKEND_URL:-}")",
  NEXT_PUBLIC_WEBSOCKET_URL: "$(sanitize "${NEXT_PUBLIC_WEBSOCKET_URL:-}")",
  NEXT_PUBLIC_ENABLE_OVH: "$(sanitize "${NEXT_PUBLIC_ENABLE_OVH:-}")",
  NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH: "$(sanitize "${NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH:-}")",
  NEXT_PUBLIC_ENABLE_SHAREPOINT: "$(sanitize "${NEXT_PUBLIC_ENABLE_SHAREPOINT:-}")",
  NEXT_PUBLIC_ENABLE_JIRA: "$(sanitize "${NEXT_PUBLIC_ENABLE_JIRA:-}")",
  NEXT_PUBLIC_ENABLE_NOTION: "$(sanitize "${NEXT_PUBLIC_ENABLE_NOTION:-}")",
  NEXT_PUBLIC_ENABLE_SPINNAKER: "$(sanitize "${NEXT_PUBLIC_ENABLE_SPINNAKER:-}")",
};
JSEOF

exec "$@"
