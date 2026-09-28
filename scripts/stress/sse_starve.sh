#!/usr/bin/env bash
# Open N incident SSE streams, then time the liveness probe while they are open.
# Streams beyond the per-process cap get a 200 text/event-stream body that only
# says "retry: 15000" and ends (EventSource gives up for good on any non-200).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/common.sh"
N="${1:-64}"
WORKERS=$(server_setting --workers); THREADS=$(server_setting --threads)
CAP=$(_container_env SSE_MAX_STREAMS_PER_PROCESS)
echo "server: workers=${WORKERS:-?} threads=${THREADS:-?} SSE_MAX_STREAMS_PER_PROCESS=${CAP:-unset(8)}  opening $N streams at $BASE/api/incidents/stream"
tmp=$(mktemp -d); pids=()
for i in $(seq 1 "$N"); do
  ( curl -s -N -m 120 -w '\nhttp=%{http_code}\n' "${auth_headers[@]}" "$BASE/api/incidents/stream" > "$tmp/$i.out" 2>/dev/null ) &
  pids+=($!)
done
python3 -c 'import time; time.sleep(4)'
accepted=$(grep -l '^retry: 2000' "$tmp"/*.out 2>/dev/null | wc -l | tr -d ' ')
refused=$(grep -l '^retry: 15000' "$tmp"/*.out 2>/dev/null | wc -l | tr -d ' ')
refused_closed=$(grep -l '^http=200' $(grep -l '^retry: 15000' "$tmp"/*.out 2>/dev/null) 2>/dev/null | wc -l | tr -d ' ')
other=$(grep -L '^retry: ' "$tmp"/*.out 2>/dev/null | wc -l | tr -d ' ')
echo "streams open (retry: 2000, holding a thread): $accepted   refused (200 + retry: 15000, ended): $refused (closed: $refused_closed)   other: $other"
fails=0
for k in 1 2 3 4 5; do r=$(probe /health/liveness); echo "liveness: $r"; [ "${r%% *}" != "200" ] && fails=$((fails+1)); done
r=$(probe /health/readiness); echo "readiness: $r"
kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; rm -rf "$tmp"
if [ $fails -eq 0 ] && [ "$refused" = "$refused_closed" ]; then echo "PASS: liveness answered with $accepted streams open; every refused stream ended with 200"; else echo "FAIL: liveness failed $fails/5 with $accepted streams open; refused=$refused closed=$refused_closed"; fi
