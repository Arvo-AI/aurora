#!/usr/bin/env bash
# Show Postgres max_connections, live connections, and the budget implied by the config.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; . "$HERE/common.sh"
echo "max_connections: $(pgq 'SHOW max_connections')"
echo "connections now: $(pgq "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")"
echo "by client:"; pgq "SELECT coalesce(client_addr::text,'local') || '  ' || state || '  ' || count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY 1,2 ORDER BY 3 DESC" 2>/dev/null | sed 's/^/  /'
W=$(server_setting --workers); T=$(server_setting --threads); P=$(_container_env DB_POOL_MAX); C=$(_container_env CELERY_CONCURRENCY)
W=${W:-2}; T=${T:-32}; P=${P:-20}; C=${C:-4}
echo "config: gunicorn workers=$W threads=$T, DB_POOL_MAX=$P, CELERY_CONCURRENCY=$C"
budget=$(( W*(P+15) + C*P + P + P + 10 ))
echo "budget at one replica of each service: $budget  (server $((W*(P+15))) + worker $((C*P)) + chatbot $P + beat $P + mcp 10)"
[ "$T" -le "$P" ] && echo "threads <= pool: ok" || echo "WARN: threads ($T) > pool ($P): request threads can queue on the pool and starve the probes"
