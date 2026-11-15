#!/usr/bin/env bash
# Shared settings and helpers for the preview scripts. Source, do not run.
#
# A "preview" is a throwaway copy of the whole Aurora stack, built from the
# production images, with its own Compose project, its own volumes and its own
# env file. Nothing here reads or writes your .env or your dev volumes.
#
# Written for bash 3.2 so it runs unchanged on macOS and on CI runners.

PREVIEW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Compose project name. Volumes are prefixed with it, which is what keeps the
# preview's Postgres and Vault data apart from a dev stack's.
PREVIEW_PROJECT="${PREVIEW_PROJECT:-aurora-preview}"
PREVIEW_ENV_FILE="${PREVIEW_ENV_FILE:-$PREVIEW_ROOT/.env.preview}"
PREVIEW_COMPOSE_FILE="${PREVIEW_COMPOSE_FILE:-$PREVIEW_ROOT/docker-compose.prod-local.yml}"
PREVIEW_EVIDENCE_DIR="${PREVIEW_EVIDENCE_DIR:-$PREVIEW_ROOT/preview-evidence}"

PREVIEW_APP_URL="${PREVIEW_APP_URL:-http://localhost:3000}"
PREVIEW_API_URL="${PREVIEW_API_URL:-http://localhost:5080}"
PREVIEW_VAULT_URL="${PREVIEW_VAULT_URL:-http://localhost:8200}"

info() { printf '\033[34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Every docker compose call goes through here so all scripts agree on the
# project, the env file and the compose file.
compose() {
  docker compose -p "$PREVIEW_PROJECT" --env-file "$PREVIEW_ENV_FILE" \
    --project-directory "$PREVIEW_ROOT" -f "$PREVIEW_COMPOSE_FILE" "$@"
}

# env_get KEY: print the value of KEY from the preview env file (empty if unset).
env_get() {
  { grep -E "^$1=" "$PREVIEW_ENV_FILE" 2>/dev/null || true; } | head -1 | cut -d= -f2-
}

# env_set KEY VALUE: replace KEY in the preview env file, or append it.
env_set() {
  local tmp
  tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" '
    BEGIN { FS = "="; done = 0 }
    $1 == k { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$PREVIEW_ENV_FILE" > "$tmp"
  mv "$tmp" "$PREVIEW_ENV_FILE"
}

# http_code URL: print the HTTP status, or 000 when nothing answers.
http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true
}

# port_open PORT: succeed when something accepts TCP connections on localhost.
port_open() {
  python3 -c 'import socket, sys
socket.create_connection(("127.0.0.1", int(sys.argv[1])), 3).close()' "$1" 2>/dev/null
}

# redact: filter stdin, replacing every secret value found in the preview env
# file. Logs are uploaded as CI artifacts, and vault-init prints the root token.
redact() {
  local args=() key value
  if [ -f "$PREVIEW_ENV_FILE" ]; then
    while IFS='=' read -r key value; do
      case "$key" in
        *PASSWORD*|*SECRET*|*TOKEN*|*API_KEY*) ;;
        *) continue ;;
      esac
      # Only real secret-shaped values: skips blanks and inline comments.
      if [[ "$value" =~ ^[A-Za-z0-9._-]{16,}$ ]]; then
        args+=(-e "s/$value/[REDACTED:$key]/g")
      fi
    done < "$PREVIEW_ENV_FILE"
  fi
  if [ "${#args[@]}" -eq 0 ]; then cat; else sed "${args[@]}"; fi
}
