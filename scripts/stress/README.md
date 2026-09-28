# Pressure tests for the API server probes and the Postgres connection budget

Reproduces the two failure modes seen on self-hosted installs (server pod killed by
its liveness probe under load; Postgres "too many clients") against a running
stack, and shows the fix holds. Nothing here needs Kubernetes: point `BASE` at any
reachable API server. Each script prints PASS/FAIL with the raw numbers.

Environment (all optional):

| Var | Default | Meaning |
|---|---|---|
| `BASE` | `http://localhost:5080` | API server |
| `X_USER_ID`, `X_ORG_ID`, `X_INTERNAL_SECRET` | from `aurora-server` container env / DB | headers for authenticated routes |
| `PG_CONTAINER` | `aurora-postgres` | container to `psql` into |
| `PROBE_TIMEOUT` | `5` | seconds a probe may take (the old kubelet timeout) |

1. `probe_watch.sh [seconds]` -- polls `/health/liveness` and `/health/readiness`
   once a second and counts answers slower than `PROBE_TIMEOUT`. Run it in a second
   terminal during any other test.
2. `sse_starve.sh [streams]` -- opens N incident SSE streams (one gunicorn thread
   each), then times the liveness probe. Before the fix every stream is accepted and
   liveness times out once N >= workers x threads. After the fix at most
   `SSE_MAX_STREAMS_PER_PROCESS` (default 8, never more than threads - 2) streams
   per worker are accepted, the rest get a 200 `text/event-stream` body that only
   says `retry: 15000` and ends (EventSource reconnects after a clean end of
   stream but gives up for good on any non-200), and liveness answers in
   milliseconds.
3. `pool_exhaust.sh [requests]` -- holds an exclusive lock on `incidents` and fires
   N concurrent `GET /api/incidents`, which park on the lock holding a pooled
   connection each. Before the fix readiness waits the full pool timeout (5s) and
   fails; after the fix it answers within ~1s as `degraded` (HTTP 200).
   Size N to exhaust the pool without exhausting the threads, e.g. with
   `DB_POOL_MAX=8` and 32 threads use N=24 (12 per worker: 8 hold connections, 4
   wait on the pool, 20 threads stay free). With N above workers x threads the
   probes queue for a thread, which is a different failure (see the note below).
4. `conn_budget.sh` -- prints Postgres `max_connections`, live connections per client
   and the budget implied by the running config, so you can see the headroom.

## What these tests cannot fix

Probes are served by the same gunicorn thread pool as requests. A burst of slow
requests larger than `workers x threads` leaves no thread for any probe until the
burst drains; nothing in the readiness handler can change that. The chart covers it
two ways: liveness now tolerates 120s of that (6 x 20s, 10s timeout), and
`server.probes.liveness.type: tcp` makes liveness independent of request threads
entirely. Keeping `GUNICORN_THREADS <= DB_POOL_MAX` bounds how long a slow-DB burst
holds threads (the pool wait is 5s), and the SSE cap removes the one thread sink that
never drains on its own.
