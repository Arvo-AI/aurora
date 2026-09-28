#!/usr/bin/env bash
# Open N incident SSE streams, then time the liveness probe while they are open.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/common.sh"
N="${1:-64}"
WORKERS=$(server_setting --workers); THREADS=$(server_setting --threads)
echo "server: workers=${WORKERS:-?} threads=${THREADS:-?}  opening $N streams at $BASE/api/incidents/stream"
tmp=$(mktemp -d); pids=()
for i in $(seq 1 "$N"); do
  ( curl -s -N -m 120 -o /dev/null -w '%{http_code}\n' "${auth_headers[@]}" "$BASE/api/incidents/stream" > "$tmp/$i.code" 2>/dev/null ) &
  pids+=($!)
done
python3 -c 'import time; time.sleep(4)'
accepted=0; refused=0
for i in $(seq 1 "$N"); do
  c=$(cat "$tmp/$i.code" 2>/dev/null)
  if [ -z "$c" ]; then accepted=$((accepted+1)); elif [ "$c" = "503" ]; then refused=$((refused+1)); fi
done
echo "streams still open (200, holding a thread): $accepted   refused with 503: $refused"
fails=0
for k in 1 2 3 4 5; do r=$(probe /health/liveness); echo "liveness: $r"; [ "${r%% *}" != "200" ] && fails=$((fails+1)); done
r=$(probe /health/readiness); echo "readiness: $r"
kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; rm -rf "$tmp"
if [ $fails -eq 0 ]; then echo "PASS: liveness answered with $accepted streams open"; else echo "FAIL: liveness failed $fails/5 with $accepted streams open"; fi
