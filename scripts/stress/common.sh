#!/usr/bin/env bash
# Shared helpers for the stress scripts. Source, do not run.
BASE="${BASE:-http://localhost:5080}"
PG_CONTAINER="${PG_CONTAINER:-aurora-postgres}"
SERVER_CONTAINER="${SERVER_CONTAINER:-aurora-server}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-5}"

_container_env() { docker inspect "$SERVER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | awk -F= -v k="$1" '$1==k {sub(/^[^=]*=/,""); print; exit}'; }

# pgq "<sql>" -> unaligned, tuples-only output from the stack's Postgres
pgq() { printf '%s\n' "$1" | docker exec -i "$PG_CONTAINER" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq' 2>/dev/null; }

X_INTERNAL_SECRET="${X_INTERNAL_SECRET:-$(_container_env INTERNAL_API_SECRET)}"
if [ -z "${X_USER_ID:-}" ] || [ -z "${X_ORG_ID:-}" ]; then
  # Any user that belongs to an org. Override with X_USER_ID / X_ORG_ID.
  _row=$(pgq "SELECT id || '|' || org_id FROM users WHERE org_id IS NOT NULL LIMIT 1")
  X_USER_ID="${X_USER_ID:-${_row%%|*}}"
  X_ORG_ID="${X_ORG_ID:-${_row##*|}}"
fi
auth_headers=(-H "X-User-ID: $X_USER_ID" -H "X-Org-ID: $X_ORG_ID" -H "X-Internal-Secret: $X_INTERNAL_SECRET")

# probe <path> -> "<http_code> <seconds>"; 000 = no answer inside PROBE_TIMEOUT
probe() { curl -s -o /dev/null -m "$PROBE_TIMEOUT" -w '%{http_code} %{time_total}' "$BASE$1"; echo; }

server_setting() { docker exec "$SERVER_CONTAINER" sh -c "ps -o args= -p 1 | tr ' ' '\n' | grep -A1 -- '$1' | tail -1" 2>/dev/null; }
