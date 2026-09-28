#!/usr/bin/env bash
# Exhaust the server's DB pool (requests parked on a table lock) and time readiness.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/common.sh"
N="${1:-48}"; HOLD="${HOLD:-35}"
POOL=$(_container_env DB_POOL_MAX); W=$(server_setting --workers)
echo "server DB_POOL_MAX=${POOL:-20 (default)} workers=${W:-?}; parking $N requests on a locked incidents table for ${HOLD}s"
pgq "BEGIN; LOCK TABLE incidents IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($HOLD); COMMIT;" >/dev/null &
lockpid=$!
python3 -c 'import time; time.sleep(1.5)'
pids=()
for i in $(seq 1 "$N"); do
  ( curl -s -o /dev/null -m "$((HOLD+10))" "${auth_headers[@]}" "$BASE/api/incidents" ) & pids+=($!)
done
python3 -c 'import time; time.sleep(3)'
echo "sessions waiting on the lock: $(pgq "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")"
worst=0; codes=""
for k in 1 2 3 4 5; do r=$(probe /health/readiness); echo "readiness: $r"; codes="$codes ${r%% *}"; t=${r##* }; worst=$(awk -v a="$worst" -v b="$t" 'BEGIN{print (b+0>a+0)?b:a}'); done
r=$(probe /health/liveness); echo "liveness: $r"
wait $lockpid 2>/dev/null; kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null
if awk -v w="$worst" -v m="$PROBE_TIMEOUT" 'BEGIN{exit !(w+0 < m-0.5)}' && ! echo "$codes" | grep -qE "503|000"; then echo "PASS: readiness answered 200 in <= ${worst}s with the pool exhausted"; else echo "FAIL: readiness worst=${worst}s codes=$codes"; fi
