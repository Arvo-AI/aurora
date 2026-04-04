#!/usr/bin/env bash
# Vault token auto-extraction and targeted service restart.
# Source this file; do not execute directly.

[[ -n "${_AURORA_VAULT_LOADED:-}" ]] && return 0
_AURORA_VAULT_LOADED=1

# Wait for vault-init to finish, extract root token, patch .env, restart dependent services.
# Args: $1 = compose file, $2 = repo root
auto_vault() {
  local compose_file="$1" repo_root="$2"
  local timeout=120 elapsed=0 token=""

  info "Waiting for Vault initialization..."

  while [[ $elapsed -lt $timeout ]]; do
    token=$(docker exec aurora-vault cat /vault/init/keys.json 2>/dev/null | \
      python3 -c "import sys,json; print(json.load(sys.stdin)['root_token'])" 2>/dev/null || true)

    if [[ -z "$token" ]]; then
      token=$(docker exec aurora-vault cat /vault/init/keys.json 2>/dev/null | \
        grep -o '"root_token":"[^"]*"' 2>/dev/null | cut -d'"' -f4 || true)
    fi

    [[ -n "$token" ]] && break
    sleep 5
    elapsed=$((elapsed + 5))
    printf "\r  Waiting for vault-init... %ds / %ds" "$elapsed" "$timeout"
  done
  echo ""

  if [[ -z "$token" ]]; then
    warn "Could not auto-extract Vault token after ${timeout}s."
    warn "Extract it manually:"
    echo "  VAULT_TOKEN=\$(docker exec aurora-vault cat /vault/init/keys.json | jq -r '.root_token')"
    echo "  sed -i \"s|^VAULT_TOKEN=.*|VAULT_TOKEN=\$VAULT_TOKEN|\" .env"
    echo "  docker compose -f $compose_file restart aurora-server celery_worker celery_beat chatbot"
    return 1
  fi

  ok "Vault token extracted"

  cd "$repo_root"
  if grep -q "^VAULT_TOKEN=" .env 2>/dev/null; then
    sed -i.bak "s|^VAULT_TOKEN=.*|VAULT_TOKEN=${token}|" .env
  else
    echo "VAULT_TOKEN=${token}" >> .env
  fi
  rm -f .env.bak

  info "Restarting services with Vault token..."
  docker compose -f "$compose_file" restart aurora-server celery_worker celery_beat chatbot 2>/dev/null || \
    docker compose -f "$compose_file" restart 2>/dev/null || true
  ok "Services restarted with Vault token"
}
