#!/usr/bin/env bash
# Post-deploy health gate.
# Source this file; do not execute directly.

[[ -n "${_AURORA_HEALTH_LOADED:-}" ]] && return 0
_AURORA_HEALTH_LOADED=1

# Poll the API health endpoint until it responds 200 or timeout.
# Args: $1 = compose file
health_gate() {
  local compose_file="$1"
  local timeout=120 elapsed=0

  info "Verifying deployment health..."

  while [[ $elapsed -lt $timeout ]]; do
    if curl -sf --connect-timeout 3 http://localhost:5080/health/liveness &>/dev/null; then
      ok "API is healthy"
      echo ""
      _print_service_status "$compose_file"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
    printf "\r  Waiting for API... %ds / %ds" "$elapsed" "$timeout"
  done
  echo ""

  warn "API did not respond within ${timeout}s (it may still be starting)."
  echo ""
  _print_service_status "$compose_file"
  return 1
}

_print_service_status() {
  local compose_file="$1"
  local services
  services=$(docker compose -f "$compose_file" ps --format "table {{.Name}}\t{{.Status}}" 2>/dev/null || true)
  if [[ -n "$services" ]]; then
    info "Service status:"
    echo "$services"
  fi
}
