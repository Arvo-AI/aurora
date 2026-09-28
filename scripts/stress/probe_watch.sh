#!/usr/bin/env bash
# Poll liveness + readiness once a second for N seconds; count slow/failed answers.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/common.sh"
SECONDS_TO_RUN="${1:-60}"
slow_live=0; slow_ready=0; bad_ready=0; n=0
end=$((SECONDS + SECONDS_TO_RUN))
printf "%-10s %-16s %-16s\n" "t" "liveness" "readiness"
while [ $SECONDS -lt $end ]; do
  l=$(probe /health/liveness); r=$(probe /health/readiness)
  printf "%-10s %-16s %-16s\n" "$(date +%H:%M:%S)" "$l" "$r"
  lc=${l%% *}; lt=${l##* }; rc=${r%% *}; rt=${r##* }
  [ "$lc" != "200" ] && slow_live=$((slow_live+1))
  [ "$rc" != "200" ] && bad_ready=$((bad_ready+1))
  awk -v t="$rt" -v m="$PROBE_TIMEOUT" 'BEGIN{exit !(t+0 >= m-0.05)}' && slow_ready=$((slow_ready+1))
  n=$((n+1)); python3 -c 'import time; time.sleep(1)'
done
echo "samples=$n liveness_failures=$slow_live readiness_non200=$bad_ready readiness_at_timeout=$slow_ready"
[ $slow_live -eq 0 ] && echo "PASS: liveness never failed" || echo "FAIL: liveness failed $slow_live times"
