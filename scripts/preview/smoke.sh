#!/usr/bin/env bash
# Deterministic checks against a running preview stack. No AI and no browser:
# these are the cheap questions that must have a yes before anything smarter
# is worth running. "Did it boot, is it wired together, are the doors locked."
#
# Usage:
#   scripts/preview/smoke.sh        (after scripts/preview/up.sh)
#
# Writes preview-evidence/summary.md and exits 1 if any check failed.
# WARN never fails the run; it marks something to look at and tighten later.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

[ -f "$PREVIEW_ENV_FILE" ] || die "no preview env file; run scripts/preview/up.sh first"

mkdir -p "$PREVIEW_EVIDENCE_DIR"
RESULTS="$PREVIEW_EVIDENCE_DIR/smoke.tsv"
: > "$RESULTS"
failures=0
warnings=0

# record STATUS NAME DETAIL
record() {
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$RESULTS"
  case "$1" in
    PASS) printf '  \033[32mPASS\033[0m  %-34s %s\n' "$2" "$3" ;;
    WARN) printf '  \033[33mWARN\033[0m  %-34s %s\n' "$2" "$3"; warnings=$((warnings + 1)) ;;
    FAIL) printf '  \033[31mFAIL\033[0m  %-34s %s\n' "$2" "$3"; failures=$((failures + 1)) ;;
  esac
}

# expect_http NAME URL WANTED_STATUS
expect_http() {
  local got
  got="$(http_code "$2")"
  if [ "$got" = "$3" ]; then
    record PASS "$1" "HTTP $got"
  else
    record FAIL "$1" "expected HTTP $3, got $got"
  fi
}

# health_status CHECK: status of one dependency as reported by the API.
health_status() {
  { curl -s --max-time 15 "$PREVIEW_API_URL/health/" 2>/dev/null || true; } \
    | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin)["checks"][sys.argv[1]]["status"])
except Exception:
    print("unreachable")' "$1"
}

# --- Containers ----------------------------------------------------------------
info "Containers"
container_count=0
crashed=""
restarted=""
for id in $(docker ps -aq --filter "label=com.docker.compose.project=$PREVIEW_PROJECT"); do
  container_count=$((container_count + 1))
  read -r name status exit_code restarts \
    <<< "$(docker inspect -f '{{.Name}} {{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}' "$id")"
  name="${name#/}"
  if [ "$status" = "exited" ] && [ "$exit_code" != "0" ]; then
    crashed="$crashed $name(exit $exit_code)"
  elif [ "$status" = "restarting" ] || [ "$status" = "dead" ] || [ "$restarts" -ge 3 ]; then
    crashed="$crashed $name($status, $restarts restarts)"
  elif [ "$restarts" -gt 0 ]; then
    restarted="$restarted $name($restarts)"
  fi
done
if [ "$container_count" -eq 0 ]; then
  record FAIL "containers" "no preview containers found"
elif [ -n "$crashed" ]; then
  record FAIL "containers" "crashed:$crashed"
else
  record PASS "containers" "$container_count containers, none crashed"
fi
if [ -n "$restarted" ]; then
  record WARN "container restarts" "restarted during boot:$restarted"
fi

# --- Backend and its dependencies ----------------------------------------------
info "Backend"
expect_http "api liveness" "$PREVIEW_API_URL/health/liveness" 200
expect_http "api readiness" "$PREVIEW_API_URL/health/readiness" 200

curl -s --max-time 15 "$PREVIEW_API_URL/health/" > "$PREVIEW_EVIDENCE_DIR/health.json" 2>/dev/null || true
for dep in database redis chatbot_websocket; do
  got="$(health_status "$dep")"
  if [ "$got" = "healthy" ]; then
    record PASS "health: $dep" "$got"
  else
    record FAIL "health: $dep" "$got"
  fi
done

# The worker registers with the broker a little after the API comes up, and a
# stack with no worker cannot run an investigation, so wait for it and insist.
celery="unreachable"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
  celery="$(health_status celery)"
  [ "$celery" = "healthy" ] && break
  sleep 5
done
if [ "$celery" = "healthy" ]; then
  record PASS "health: celery worker" "$celery"
else
  record FAIL "health: celery worker" "$celery after 90s"
fi

for port_check in "chatbot websocket:5006" "mcp server:$(env_get MCP_PORT)"; do
  label="${port_check%%:*}"
  port="${port_check##*:}"
  port="${port:-8811}"
  if port_open "$port"; then
    record PASS "port: $label" "accepting connections on $port"
  else
    record FAIL "port: $label" "nothing listening on $port"
  fi
done

# --- Frontend ------------------------------------------------------------------
info "Frontend"
expect_http "frontend sign-in page" "$PREVIEW_APP_URL/sign-in" 200
expect_http "frontend ping route" "$PREVIEW_APP_URL/api/ping" 200

# The image is built once and configured at start: docker-entrypoint.sh writes
# the public settings into env-config.js. An empty value means the browser
# would not know where the backend is.
backend_url="$(env_get NEXT_PUBLIC_BACKEND_URL)"
env_config="$(curl -s --max-time 10 "$PREVIEW_APP_URL/env-config.js" 2>/dev/null || true)"
case "$env_config" in
  *"\"$backend_url\""*) record PASS "frontend runtime config" "env-config.js carries the backend URL" ;;
  *) record FAIL "frontend runtime config" "env-config.js is missing NEXT_PUBLIC_BACKEND_URL=$backend_url" ;;
esac

# --- Trust boundaries ----------------------------------------------------------
# Cheap to check and expensive to get wrong: these are the invariants in
# CLAUDE.md ("Client <-> Backend Boundary") tested from the outside.
info "Trust boundaries"
expect_http "frontend API refuses anonymous" "$PREVIEW_APP_URL/api/getUserId" 401
expect_http "backend refuses calls without the internal secret" "$PREVIEW_API_URL/api/incidents" 403

redirect="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 10 "$PREVIEW_APP_URL/" 2>/dev/null || true)"
case "$redirect" in
  30?\ *"/sign-in"*) record PASS "anonymous visitor sent to sign-in" "HTTP ${redirect%% *}" ;;
  *) record FAIL "anonymous visitor sent to sign-in" "got '$redirect'" ;;
esac

# --- Vault ---------------------------------------------------------------------
# Write, read back and delete one secret with the token the application uses.
# A wrong token or a sealed Vault boots fine and then fails the first time a
# user connects a cloud account, which is why it is checked here.
info "Vault"
vault_token="$(env_get VAULT_TOKEN)"
vault_mount="$(env_get VAULT_KV_MOUNT)"
vault_base="$(env_get VAULT_KV_BASE_PATH)"
secret_path="${vault_mount:-aurora}/data/${vault_base:-users}/preview-smoke"
meta_path="${vault_mount:-aurora}/metadata/${vault_base:-users}/preview-smoke"
probe="smoke-$$-$SECONDS"
write_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H "X-Vault-Token: $vault_token" -H 'Content-Type: application/json' \
  -X POST -d "{\"data\":{\"value\":\"$probe\"}}" \
  "$PREVIEW_VAULT_URL/v1/$secret_path" 2>/dev/null || true)"
read_body="$(curl -s --max-time 10 -H "X-Vault-Token: $vault_token" \
  "$PREVIEW_VAULT_URL/v1/$secret_path" 2>/dev/null || true)"
curl -s -o /dev/null --max-time 10 -H "X-Vault-Token: $vault_token" \
  -X DELETE "$PREVIEW_VAULT_URL/v1/$meta_path" 2>/dev/null || true
case "$read_body" in
  *"$probe"*) record PASS "vault secret round trip" "wrote and read back with the app's token" ;;
  *) record FAIL "vault secret round trip" "write returned HTTP $write_code, value not read back" ;;
esac

# --- Logs ----------------------------------------------------------------------
# A Python traceback during a clean boot is a defect, but today's baseline is
# unknown, so this starts as a warning. Once it reads zero on main, make it FAIL.
info "Logs"
traceback_report=""
for id in $(docker ps -aq --filter "label=com.docker.compose.project=$PREVIEW_PROJECT"); do
  image="$(docker inspect -f '{{.Config.Image}}' "$id")"
  case "$image" in
    aurora_*) ;;
    *) continue ;;
  esac
  count="$(docker logs "$id" 2>&1 | grep -c 'Traceback (most recent call last)' || true)"
  if [ "${count:-0}" -gt 0 ]; then
    name="$(docker inspect -f '{{.Name}}' "$id")"
    traceback_report="$traceback_report ${name#/}($count)"
  fi
done
if [ -n "$traceback_report" ]; then
  record WARN "python tracebacks in logs" "$traceback_report"
else
  record PASS "python tracebacks in logs" "none"
fi

# --- Summary -------------------------------------------------------------------
SUMMARY="$PREVIEW_EVIDENCE_DIR/summary.md"
total="$(wc -l < "$RESULTS" | tr -d ' ')"
{
  if [ "$failures" -gt 0 ]; then
    echo "### Preview smoke checks: FAILED ($failures of $total)"
  else
    echo "### Preview smoke checks: passed ($total checks, $warnings warnings)"
  fi
  echo
  echo "| Result | Check | Detail |"
  echo "|---|---|---|"
  while IFS="$(printf '\t')" read -r status name detail; do
    echo "| $status | $name | $detail |"
  done < "$RESULTS"
} > "$SUMMARY"

echo
if [ "$failures" -gt 0 ]; then
  die "$failures of $total smoke checks failed (summary: $SUMMARY)"
fi
info "All $total smoke checks passed ($warnings warnings). Summary: $SUMMARY"
