#!/usr/bin/env bash
# Boot a throwaway Aurora stack from the production images and wait until it
# answers. This is the same path as `make init && make prod-local`, made
# unattended: it generates secrets, initialises Vault, feeds the Vault root
# token back into the env file, and only then starts the application services.
#
# Usage:
#   scripts/preview/up.sh
#
# Options (environment variables):
#   PREVIEW_BUILD=0          use images that already exist instead of building
#                            (CI builds them once with a layer cache and tags them)
#   PREVIEW_BOOT_TIMEOUT=600 seconds to wait for the API and the frontend
#
# Stop any other Aurora stack first (`make down`): container names and host
# ports are fixed in the compose file, so two stacks cannot run side by side.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BUILD="${PREVIEW_BUILD:-1}"
BOOT_TIMEOUT="${PREVIEW_BOOT_TIMEOUT:-600}"

command -v docker >/dev/null || die "docker is not installed"
command -v python3 >/dev/null || die "python3 is not installed"

# Both Dockerfiles use `RUN --mount=type=cache`, which only BuildKit
# understands. Docker's older builder stops partway with "the --mount option
# requires BuildKit", so check up front and ask for BuildKit explicitly.
if [ "$BUILD" = "1" ]; then
  docker buildx version >/dev/null 2>&1 || die "the docker buildx plugin is missing, and the images need BuildKit to build.
       Docker Desktop ships it. With Homebrew's docker (Colima and similar): brew install docker-buildx,
       then follow the 'cli-plugins' note it prints."
  export DOCKER_BUILDKIT=1
fi

# --- 1. Refuse to collide with another Aurora stack --------------------------
others="$(docker ps --filter 'name=^aurora-' \
  --format '{{.Names}} {{.Label "com.docker.compose.project"}}' \
  | awk -v p="$PREVIEW_PROJECT" '$2 != p { print $1 }')"
if [ -n "$others" ]; then
  die "another Aurora stack is running (run 'make down' first): $(echo $others)"
fi

# --- 2. Env file with generated secrets --------------------------------------
# Created once and then reused: the Postgres volume is initialised with the
# password in this file, so regenerating it against an existing volume would
# lock the stack out of its own database. down.sh removes both together.
if [ ! -f "$PREVIEW_ENV_FILE" ]; then
  info "Creating $(basename "$PREVIEW_ENV_FILE") from .env.example"
  cp "$PREVIEW_ROOT/.env.example" "$PREVIEW_ENV_FILE"
  (cd "$PREVIEW_ROOT" && ENV_FILE="$PREVIEW_ENV_FILE" ./scripts/generate-local-secrets.sh >/dev/null)
else
  info "Reusing $(basename "$PREVIEW_ENV_FILE")"
fi

# --- 3. Vault first -----------------------------------------------------------
# The application reads VAULT_TOKEN from its environment, but the token only
# exists after Vault has been initialised. So: start Vault, let vault-init
# initialise and unseal it, read the root token, then start everything else.
info "Starting Vault"
compose up -d vault vault-init

init_container="$(compose ps -aq vault-init)"
[ -n "$init_container" ] || die "vault-init container was not created"
init_exit="$(docker wait "$init_container")"
if [ "$init_exit" != "0" ]; then
  # Never print lines that could carry the root token.
  docker logs "$init_container" 2>&1 | grep -vi 'token' | tail -30 >&2 || true
  die "vault-init exited with code $init_exit"
fi

vault_token="$(compose exec -T vault cat /vault/init/keys.json \
  | sed -n 's/.*"root_token":"\([^"]*\)".*/\1/p')"
[ -n "$vault_token" ] || die "could not read the Vault root token from the vault-init volume"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  echo "::add-mask::$vault_token"
fi
env_set VAULT_TOKEN "$vault_token"
info "Vault initialised and unsealed"

# --- 4. Everything else -------------------------------------------------------
if [ "$BUILD" = "1" ]; then
  info "Building images and starting the stack (first build is slow)"
  compose up -d --build
else
  info "Starting the stack from existing images"
  compose up -d --no-build
fi

# --- 5. Wait until it answers -------------------------------------------------
# /health/ returns 200 only when Postgres, Redis and the chatbot all answer, so
# it doubles as the readiness signal for the backend half of the stack.
info "Waiting up to ${BOOT_TIMEOUT}s for the API and the frontend"
deadline=$((SECONDS + BOOT_TIMEOUT))
last_report=0
while :; do
  api="$(http_code "$PREVIEW_API_URL/health/")"
  app="$(http_code "$PREVIEW_APP_URL/sign-in")"
  if [ "$api" = "200" ] && [ "$app" = "200" ]; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    compose ps -a >&2 || true
    die "stack did not become ready in ${BOOT_TIMEOUT}s (api=$api frontend=$app)"
  fi
  if [ $((SECONDS - last_report)) -ge 30 ]; then
    echo "    api=$api frontend=$app (${SECONDS}s elapsed)"
    last_report=$SECONDS
  fi
  sleep 5
done

info "Preview is up after ${SECONDS}s"
echo "    Frontend: $PREVIEW_APP_URL"
echo "    API:      $PREVIEW_API_URL"
echo "    Next:     scripts/preview/smoke.sh"
