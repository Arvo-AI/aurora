// A self-contained sample incident for the "Paste an incident" dialog, so a
// user with nothing to paste still gets a first investigation.

export const SAMPLE_INCIDENT_TITLE = 'checkout-api 5xx error rate above 5%';

export const SAMPLE_INCIDENT_EVIDENCE = `=== ALERT (Grafana) ===
{
  "status": "firing",
  "labels": {
    "alertname": "HighErrorRate",
    "service": "checkout-api",
    "env": "production",
    "severity": "critical"
  },
  "annotations": {
    "summary": "checkout-api 5xx error rate above 5% for 5m",
    "description": "5xx ratio is 31.4% (threshold 5%). p99 latency 30.2s."
  },
  "startsAt": "2026-11-12T14:09:00Z",
  "generatorURL": "https://grafana.example.com/alerting/grafana/checkout-5xx/view"
}

=== DEPLOY LOG (CI) ===
2026-11-12T14:01:12Z deploy checkout-api v2.13.4 -> v2.14.0 (commit 9f3c2ab "perf: tune worker and pool settings for smaller pods")
2026-11-12T14:01:40Z rollout: 0/6 pods updated
2026-11-12T14:03:05Z rollout: 6/6 pods updated, all ready
2026-11-12T14:03:06Z deploy finished: success

=== APPLICATION LOGS (checkout-api) ===
2026-11-12T13:58:41Z INFO  [checkout-api-7c9d-x2k4f] POST /v1/checkout 200 142ms
2026-11-12T13:59:02Z INFO  [checkout-api-7c9d-x2k4f] POST /v1/checkout 200 151ms
2026-11-12T14:02:11Z INFO  [checkout-api-5b8f-q9w2m] starting checkout-api v2.14.0
2026-11-12T14:02:11Z INFO  [checkout-api-5b8f-q9w2m] config: WEB_CONCURRENCY=16 DB_POOL_SIZE=5 DB_MAX_OVERFLOW=0 DB_POOL_TIMEOUT=30
2026-11-12T14:02:12Z INFO  [checkout-api-5b8f-q9w2m] connected to postgres orders-db-primary:5432 (max_connections=400)
2026-11-12T14:02:14Z INFO  [checkout-api-5b8f-q9w2m] readiness probe ok
2026-11-12T14:03:31Z INFO  [checkout-api-5b8f-q9w2m] POST /v1/checkout 200 164ms
2026-11-12T14:04:02Z WARN  [checkout-api-5b8f-q9w2m] db pool: 5/5 connections checked out, 11 requests waiting
2026-11-12T14:04:18Z WARN  [checkout-api-5b8f-t4h7z] db pool: 5/5 connections checked out, 9 requests waiting
2026-11-12T14:04:32Z ERROR [checkout-api-5b8f-q9w2m] sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, timeout 30.00
2026-11-12T14:04:32Z ERROR [checkout-api-5b8f-q9w2m] POST /v1/checkout 503 30004ms
2026-11-12T14:04:33Z ERROR [checkout-api-5b8f-t4h7z] sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, timeout 30.00
2026-11-12T14:04:33Z ERROR [checkout-api-5b8f-t4h7z] POST /v1/checkout 503 30002ms
2026-11-12T14:05:10Z WARN  [checkout-api-5b8f-k1p8d] db pool: 5/5 connections checked out, 14 requests waiting
2026-11-12T14:05:47Z ERROR [checkout-api-5b8f-k1p8d] POST /v1/cart/apply-coupon 503 30001ms
2026-11-12T14:06:15Z INFO  [checkout-api-5b8f-q9w2m] GET /healthz 200 2ms
2026-11-12T14:07:02Z ERROR [checkout-api-5b8f-q9w2m] sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out, timeout 30.00
2026-11-12T14:07:02Z ERROR [checkout-api-5b8f-q9w2m] POST /v1/checkout 503 30003ms
2026-11-12T14:08:44Z WARN  [checkout-api-5b8f-t4h7z] db pool: 5/5 connections checked out, 16 requests waiting
2026-11-12T14:09:00Z ERROR [checkout-api-5b8f-k1p8d] POST /v1/checkout 503 30002ms

=== DATABASE METRICS (orders-db-primary, pasted from dashboard) ===
time      active_connections  cpu_pct  replication_lag_s
13:55     212                 34       0.2
14:00     208                 33       0.2
14:05     31                  9        0.1
14:10     30                  8        0.1
`;
