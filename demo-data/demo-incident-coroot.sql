-- Demo Incident (Coroot): Database connectivity issues causing cascading latency
-- Source: Coroot | Tools: coroot_get_incidents, coroot_get_incident_detail, coroot_get_app_logs,
--         coroot_get_traces, coroot_get_service_map, coroot_get_app_detail, GitHub code search

DO $$
DECLARE
  v_incident_id UUID := 'c0c00e01-db4c-4f8b-9a1d-2c5e8f3b6d91';
  v_chat_session_id VARCHAR(50) := 'c0c00e02-5e6f-7a8b-9c0d-1e2f3a4b5c6d';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_source_alert_id INTEGER := 9991;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident (Coroot) already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident (Coroot)...';

  -- 1. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'coroot', v_source_alert_id,
    'resolved', 'critical',
    'Database connectivity issues causing cascading latency - catalog service',
    'catalog', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-17 14:13:22', '2026-02-17 14:28:00',
    'thoughts', '2026-02-17 14:13:22', '2026-02-17 14:28:00',
    jsonb_build_object(
      'is_demo', true,
      'coroot_incident_key', 'kf0x7ljs',
      'coroot_project_id', 'tbuzvelk',
      'incidentId', 'kf0x7ljs',
      'source_url', 'https://demo.coroot.com/p/tbuzvelk/incidents?incident=kf0x7ljs',
      'slo_breach', 'Latency: 98.96% (target: 99.9% < 500ms)',
      'duration_minutes', 24,
      'raw_payload', $raw${"incident_key":"kf0x7ljs","project_id":"tbuzvelk","project_name":"acme-prod","application":"default/Deployment/catalog","severity":"critical","status":"open","started_at":"2026-02-17T14:13:22Z","resolved_at":"2026-02-17T14:37:23Z","duration_seconds":1441,"slos":[{"name":"Availability","objective":"99.9% of requests should not fail","compliance_actual":"100%","compliance_target":"99.9%","breached":false},{"name":"Latency","objective":"99.9% of requests < 500ms","compliance_actual":"98.96%","compliance_target":"99.9%","breached":true,"error_budget_burn_rate":{"short_window_15m":8.2,"long_window_6h":10.1,"alert_threshold":6.0}}],"root_cause":"The catalog service is experiencing database connectivity issues with db-main (PostgreSQL). Connection failures and timeouts are causing 10.4% error rate on catalog, cascading to front-end and order services.","affected_applications":[{"name":"default/Deployment/front-end","status":"degraded","error_rate":"0.8%"},{"name":"default/Deployment/catalog","status":"critical","error_rate":"10.4%"},{"name":"default/Deployment/order","status":"degraded","error_rate":"2.1%"},{"name":"default/StatefulSet/db-main","status":"warning","type":"postgresql"}],"metrics_at_trigger":{"catalog_p99_latency_ms":1247,"catalog_p95_latency_ms":850,"catalog_error_rate_pct":10.4,"catalog_db_connections_active":10,"catalog_db_connections_max":10,"catalog_db_connections_waiting":47,"front_end_p99_latency_ms":1200,"front_end_slo_compliance_pct":98.96},"webhook_url":"https://your-aurora-instance/api/coroot/webhook","webhook_version":"1","notification_id":"notif-kf0x7ljs-1739802802"}$raw$
    ),
    3,
    ARRAY['catalog', 'front-end', 'order', 'db-main'],
    '', '2026-02-17 14:28:00'
  );

  -- 2. Update summary
  UPDATE incidents SET aurora_summary = '# Incident Report: Database Connectivity Issues - Catalog Service

## Summary

On February 17, 2026 at 14:13 UTC, the **catalog** service began experiencing database connectivity failures with the PostgreSQL instance **db-main** [1]. The incident lasted 24 minutes and caused a cascading failure affecting the front-end and order services [2]. The primary SLO breach was on latency: p99 compliance degraded to 98.96% against a 99.9% target (requests served under 500ms) [3].

**Root Cause Analysis** revealed that the catalog service''s database connection pool was exhausted due to improperly configured connection timeouts in `catalog-service/src/db/connection.js:42-58` [4]. The pool was initialized with a maximum of 10 connections but no timeout or lifecycle configuration [5]. Under the normal load of ~200 req/s, connections were not being released after query execution due to missing `client.release()` calls in error paths at `catalog-service/src/handlers/product-lookup.js:87-94` [6].

When one of the PostgreSQL replicas experienced a brief network partition (5-second delay) at 14:12 UTC, several connections hung indefinitely [7]. The pool exhausted all 10 connections within 30 seconds. Subsequent requests to the catalog service failed with "operation was canceled" and "context canceled" errors [8], returning HTTP 500. These cascaded upstream: the front-end service p95/p99 latency rose from 180ms to 1,200ms as requests timed out waiting for catalog responses [9], and the order service saw 15% of order placements fail due to missing inventory checks [10].

**Impact**: During the 24-minute incident window, approximately 28,800 catalog requests failed (200 req/s x 24 min x 10% error rate). Customer-facing errors increased by 12% [11].

## Suggested Next Steps

- **Scale database connection pool** (applied): Increased `max_connections` from 10 to 50, set `connection_timeout` to 5 seconds. Applied via env var update and pod restart at 14:25 UTC, restoring service health within 3 minutes [12].
- **Fix connection leak in error path**: `catalog-service/src/handlers/product-lookup.js:87-94` is missing `client.release()` in the catch block. PR #152 adds proper try-finally cleanup [13].
- **Improve pool configuration**: `catalog-service/src/db/connection.js:42-58` needs timeout, idle eviction, and max-uses settings to prevent future pool starvation.
- **Add connection pool monitoring**: Expose `pool.totalCount`, `pool.idleCount`, `pool.waitingCount` as Prometheus metrics and alert when utilization exceeds 80%.'
  WHERE id = v_incident_id;

  -- 3. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "catalog-svc", "label": "catalog service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "db-main", "label": "db-main (PostgreSQL)", "type": "database", "status": "degraded", "parentId": null},
    {"id": "front-end", "label": "front-end", "type": "service", "status": "degraded", "parentId": null},
    {"id": "order-svc", "label": "order service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "conn-pool", "label": "Connection Pool (max:10)", "type": "event", "status": "failed", "parentId": "catalog-svc"},
    {"id": "conn-leak", "label": "Missing client.release()", "type": "event", "status": "failed", "parentId": "catalog-svc"},
    {"id": "network-blip", "label": "Network Partition (5s)", "type": "event", "status": "failed", "parentId": "db-main"},
    {"id": "slo-breach", "label": "Latency SLO Breach", "type": "alert", "status": "failed", "parentId": null},
    {"id": "pr-152", "label": "PR #152: Fix connection leak", "type": "deployment", "status": "healthy", "parentId": null}
  ],
  "edges": [
    {"source": "network-blip", "target": "conn-pool", "label": "hung connections", "type": "causation"},
    {"source": "conn-leak", "target": "conn-pool", "label": "connections never returned", "type": "causation"},
    {"source": "conn-pool", "target": "catalog-svc", "label": "pool exhausted → 500s", "type": "causation"},
    {"source": "catalog-svc", "target": "front-end", "label": "timeout → p99 1200ms", "type": "causation"},
    {"source": "catalog-svc", "target": "order-svc", "label": "inventory check fails", "type": "causation"},
    {"source": "front-end", "target": "slo-breach", "label": "triggered SLO alert", "type": "causation"},
    {"source": "pr-152", "target": "conn-leak", "label": "fixes connection leak", "type": "resolution"}
  ],
  "rootCauseId": "conn-leak",
  "affectedIds": ["conn-pool", "catalog-svc", "front-end", "order-svc", "slo-breach", "network-blip"],
  "version": 5,
  "updatedAt": "2026-02-17T14:28:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  -- 4. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'coroot', v_source_alert_id, 'Latency SLO breach on front-end (98.96% < 99.9%)', 'front-end', 'critical', 'primary', 1.0, '{}', '{"tags":["env:production","service:front-end","slo:latency"]}'::jsonb, '2026-02-17 14:13:22'),
  (v_demo_user_id, v_incident_id, 'coroot', v_source_alert_id + 1, 'catalog service: database connection failures to db-main', 'catalog', 'critical', 'topology', 0.96, '{"reason":"catalog is upstream dependency of front-end; connection failures to db-main correlate with SLO breach"}'::jsonb, '{"tags":["env:production","service:catalog","db:db-main"]}'::jsonb, '2026-02-17 14:13:30'),
  (v_demo_user_id, v_incident_id, 'coroot', v_source_alert_id + 2, 'order service: elevated failure rate (15%)', 'order', 'warning', 'time_proximity', 0.88, '{"reason":"order depends on catalog for inventory checks; failure rate spike at same time window"}'::jsonb, '{"tags":["env:production","service:order","dependency:catalog"]}'::jsonb, '2026-02-17 14:14:00');

  RAISE NOTICE 'Demo incident (Coroot) core record inserted.';
END $$;

-- Thoughts, citations, suggestions
DO $$
DECLARE
  v_incident_id UUID := 'c0c00e01-db4c-4f8b-9a1d-2c5e8f3b6d91';
BEGIN

  -- 5. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-17 14:13:30', 'Starting investigation of Coroot incident kf0x7ljs. SLO breach detected on front-end service: latency compliance dropped to 98.96% against a 99.9% target.', 'analysis'),
  (v_incident_id, '2026-02-17 14:14:00', 'Querying Coroot for incident details and affected applications. Need to understand the full dependency chain.', 'analysis'),
  (v_incident_id, '2026-02-17 14:14:30', 'Coroot RCA indicates database connectivity issues between catalog service and db-main PostgreSQL. Checking service map to understand dependencies.', 'analysis'),
  (v_incident_id, '2026-02-17 14:15:00', 'Service map confirms: front-end → catalog → db-main. The catalog service is the bottleneck. Also affects order service which depends on catalog for inventory checks.', 'analysis'),
  (v_incident_id, '2026-02-17 14:16:00', 'Pulling catalog service application detail from Coroot. Need to check connection metrics, error rates, and latency breakdown.', 'analysis'),
  (v_incident_id, '2026-02-17 14:17:00', 'Catalog service shows 10% error rate with "operation was canceled" and "context canceled" errors. TCP retransmissions elevated between catalog and db-main pods.', 'analysis'),
  (v_incident_id, '2026-02-17 14:18:00', 'Querying catalog service logs for error details. Filtering for severity=Error in the incident time window.', 'analysis'),
  (v_incident_id, '2026-02-17 14:19:00', 'Found 1,247 occurrences of "pq: canceling statement due to user request" in 10-minute window. Connection pool exhaustion confirmed — all 10 connections stuck.', 'analysis'),
  (v_incident_id, '2026-02-17 14:20:00', 'Checking error traces for the catalog service to see which code paths are failing.', 'analysis'),
  (v_incident_id, '2026-02-17 14:21:00', 'Traces show the product lookup endpoint (/api/products/:id) is the primary failure point. Spans show connections acquired but never released on error paths.', 'analysis'),
  (v_incident_id, '2026-02-17 14:22:00', 'Searching GitHub for the product lookup handler code to identify the exact bug.', 'analysis'),
  (v_incident_id, '2026-02-17 14:23:00', 'Found it. catalog-service/src/handlers/product-lookup.js:87-94 — the catch block handles the error but never calls client.release(). Connections leak on every failed query.', 'analysis'),
  (v_incident_id, '2026-02-17 14:24:00', 'Also found suboptimal pool config in catalog-service/src/db/connection.js:42-58 — max pool size is only 10 with no connection timeout or idle eviction.', 'analysis'),
  (v_incident_id, '2026-02-17 14:25:00', 'Immediate fix: Scale connection pool from 10 to 50 and add 5s timeout via env var update + pod restart.', 'analysis'),
  (v_incident_id, '2026-02-17 14:26:00', 'Connection pool scaled. Catalog service recovering — error rate dropping. Preparing permanent code fix.', 'analysis'),
  (v_incident_id, '2026-02-17 14:27:00', 'Creating code fix: add try-finally to ensure client.release() always runs. Also improving pool config with proper timeouts.', 'analysis'),
  (v_incident_id, '2026-02-17 14:28:00', 'Investigation complete. Immediate fix applied (pool scaling). Permanent fix submitted as PR #152 with connection leak repair and improved pool configuration.', 'analysis');

  -- 6. Insert citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Coroot', 'coroot_get_incident_detail: incident kf0x7ljs', '{
  "incident_key": "kf0x7ljs",
  "application": "front-end",
  "started": "2026-02-17T14:13:22Z",
  "resolved": "2026-02-17T14:37:23Z",
  "duration": "24m",
  "slos": [
    {"name": "Availability", "objective": "99.9% of requests should not fail", "compliance": "100%"},
    {"name": "Latency", "objective": "99.9% of requests < 500ms", "compliance": "98.96%", "breached": true}
  ],
  "root_cause": "The catalog service is experiencing database connectivity issues with db-main (PostgreSQL). Connection failures and timeouts causing 500 errors, cascading to front-end and order services."
}', '2026-02-17 14:14:00'),
  (v_incident_id, '2', 'Coroot', 'coroot_get_service_map: project tbuzvelk', '{
  "services": [
    {"name": "front-end", "status": "degraded", "dependencies": ["catalog", "order"]},
    {"name": "catalog", "status": "critical", "dependencies": ["db-main"]},
    {"name": "order", "status": "degraded", "dependencies": ["catalog"]},
    {"name": "db-main", "status": "warning", "type": "PostgreSQL"}
  ],
  "cascade_path": "db-main → catalog → front-end, order"
}', '2026-02-17 14:15:00'),
  (v_incident_id, '3', 'Coroot', 'coroot_get_app_detail: catalog service', '{
  "app_id": "tbuzvelk:default:Deployment:catalog",
  "status": "CRITICAL",
  "error_rate": "10.4%",
  "p50_latency_ms": 245,
  "p95_latency_ms": 850,
  "p99_latency_ms": 1247,
  "connections": {"active": 10, "max": 10, "waiting": 47},
  "tcp_retransmissions": "elevated between catalog and db-main pods",
  "top_errors": ["operation was canceled", "context canceled", "connection pool exhausted"]
}', '2026-02-17 14:16:00'),
  (v_incident_id, '4', 'Coroot', 'coroot_get_app_logs: catalog, severity=Error', '[
  {"timestamp": "2026-02-17T14:13:25Z", "severity": "Error", "message": "pq: canceling statement due to user request", "count": 347},
  {"timestamp": "2026-02-17T14:15:41Z", "severity": "Error", "message": "connection pool exhausted: 0 idle, 10 active, 47 waiting", "count": 412},
  {"timestamp": "2026-02-17T14:18:03Z", "severity": "Error", "message": "context canceled: connection to db-main timed out", "count": 488},
  {"total_error_entries": 1247, "window": "14:13-14:23 UTC"}
]', '2026-02-17 14:18:00'),
  (v_incident_id, '5', 'Coroot', 'coroot_get_traces: catalog, status_error=true', '{
  "error_traces": [
    {"trace_id": "abc123def456", "service": "catalog", "operation": "GET /api/products/:id", "duration_ms": 5003, "status": "ERROR", "error": "context canceled", "spans": [
      {"name": "pg.connect", "duration_ms": 5001, "status": "ERROR", "attributes": {"db.system": "postgresql", "pool.active": 10, "pool.idle": 0}},
      {"name": "product-lookup.js:lookupProduct", "duration_ms": 5003, "status": "ERROR"}
    ]},
    {"trace_id": "def789ghi012", "service": "catalog", "operation": "GET /api/products/:id", "duration_ms": 5002, "status": "ERROR", "error": "operation was canceled"}
  ],
  "summary": "All error traces show connection acquisition timeout at pg.connect span. Pool fully saturated (10/10 active, 0 idle)."
}', '2026-02-17 14:20:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '6', 'MCP: GitHub', 'github: search code "client.release" in catalog-service', '{
  "results": [
    {
      "file": "catalog-service/src/handlers/product-lookup.js",
      "lines": "87-94",
      "content": "} catch (err) {\n    console.error(''Product lookup failed:'', err);\n    res.status(500).json({ error: ''Internal server error'' });\n  }",
      "note": "Missing client.release() in catch block — connections leak on every failed query"
    },
    {
      "file": "catalog-service/src/db/connection.js",
      "lines": "42-58",
      "content": "const pool = new Pool({\n  host: process.env.DB_HOST,\n  port: process.env.DB_PORT,\n  database: process.env.DB_NAME,\n  user: process.env.DB_USER,\n  password: process.env.DB_PASSWORD,\n  max: 10,\n});",
      "note": "Pool max size is only 10 with no connectionTimeoutMillis, idleTimeoutMillis, or maxUses"
    }
  ]
}', '2026-02-17 14:22:00'),
  (v_incident_id, '7', 'MCP: GitHub', 'github: get file catalog-service/src/handlers/product-lookup.js', '{
  "file": "catalog-service/src/handlers/product-lookup.js",
  "content_excerpt_lines_80_100": "async function lookupProduct(req, res) {\n  const client = await pool.connect();\n  try {\n    const { productId } = req.params;\n    const result = await client.query(\n      ''SELECT * FROM products WHERE id = $1'',\n      [productId]\n    );\n    res.json(result.rows[0]);\n  } catch (err) {\n    console.error(''Product lookup failed:'', err);\n    res.status(500).json({ error: ''Internal server error'' });\n  }\n}",
  "analysis": "Line 87-94: The catch block logs the error and sends a 500 response, but never releases the database connection back to the pool. On every failed query, a connection is permanently leaked."
}', '2026-02-17 14:23:00'),
  (v_incident_id, '8', 'Coroot', 'coroot_get_app_detail: front-end latency during incident', '{
  "app_id": "tbuzvelk:default:Deployment:front-end",
  "latency_during_incident": {
    "p50_ms": 320, "p75_ms": 580, "p95_ms": 850, "p99_ms": 1200,
    "baseline_p99_ms": 180
  },
  "slo_compliance": "98.96%",
  "slo_target": "99.9% < 500ms",
  "error_budget_burn_rate": {"6h": 10, "15m": 8, "threshold": 6}
}', '2026-02-17 14:17:00'),
  (v_incident_id, '9', 'Cloud CLI', 'kubectl set env deployment/catalog-service MAX_DB_CONNECTIONS=50 DB_CONNECTION_TIMEOUT=5000 && kubectl rollout restart deployment/catalog-service', 'deployment.apps/catalog-service env updated
deployment.apps/catalog-service restarted

Rollout status: 3/3 replicas updated, available.
catalog-service error rate: 10.4% → 0.3% (recovered within 3 minutes)', '2026-02-17 14:25:00'),
  (v_incident_id, '10', 'MCP: GitHub', 'github: create PR #152 fix/connection-pool-leak', '{
  "status": "success",
  "action": "create_pull_request",
  "pr_number": 152,
  "pr_url": "https://github.com/acme-corp/catalog-service/pull/152",
  "title": "Fix: Add connection cleanup in product lookup error handling",
  "branch": "fix/connection-pool-leak",
  "files_changed": 2,
  "changes": [
    {"file": "src/handlers/product-lookup.js", "change": "Add try-finally block with client.release()"},
    {"file": "src/db/connection.js", "change": "Increase pool max, add connectionTimeoutMillis, idleTimeoutMillis, maxUses"}
  ]
}', '2026-02-17 14:27:00');

  -- 7. Insert suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Scale database connection pool to prevent exhaustion',
   'Increase MAX_DB_CONNECTIONS from 10 to 50 and set connection timeout to 5 seconds. This immediately prevents indefinite hangs and provides headroom for burst traffic.',
   'mitigation', 'low',
   'kubectl set env deployment/catalog-service MAX_DB_CONNECTIONS=50 DB_CONNECTION_TIMEOUT=5000 && kubectl rollout restart deployment/catalog-service',
   '2026-02-17 14:24:00'),
  (v_incident_id,
   'Verify catalog and front-end service recovery',
   'After pool scaling, confirm error rates have returned to baseline and the latency SLO is no longer breached.',
   'diagnostic', 'safe',
   'kubectl top pods -l app=catalog-service && kubectl logs -l app=catalog-service --tail=10 --since=5m | grep -c ERROR',
   '2026-02-17 14:26:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Fix connection leak in product lookup error handling',
   'The catch block in product-lookup.js:87-94 never calls client.release(), leaking a database connection on every failed query. This try-finally fix ensures the connection is always returned to the pool.',
   'fix', 'low', NULL,
   'catalog-service/src/handlers/product-lookup.js',
   E'async function lookupProduct(req, res) {\n  const client = await pool.connect();\n  try {\n    const { productId } = req.params;\n    const result = await client.query(\n      ''SELECT * FROM products WHERE id = $1'',\n      [productId]\n    );\n    res.json(result.rows[0]);\n  } catch (err) {\n    console.error(''Product lookup failed:'', err);\n    res.status(500).json({ error: ''Internal server error'' });\n  }\n}',
   E'async function lookupProduct(req, res) {\n  const client = await pool.connect();\n  try {\n    const { productId } = req.params;\n    const result = await client.query(\n      ''SELECT * FROM products WHERE id = $1'',\n      [productId]\n    );\n    res.json(result.rows[0]);\n  } catch (err) {\n    console.error(''Product lookup failed:'', err);\n    res.status(500).json({ error: ''Internal server error'' });\n  } finally {\n    client.release();\n  }\n}',
   'acme-corp/catalog-service',
   'https://github.com/acme-corp/catalog-service/pull/152',
   152,
   'fix/connection-pool-leak',
   '2026-02-17 14:27:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Improve database connection pool configuration',
   'The pool in connection.js:42-58 has max=10 with no timeout or lifecycle settings. Add connectionTimeoutMillis, idleTimeoutMillis, and maxUses to prevent future pool starvation and improve connection hygiene.',
   'fix', 'low', NULL,
   'catalog-service/src/db/connection.js',
   E'const pool = new Pool({\n  host: process.env.DB_HOST,\n  port: process.env.DB_PORT,\n  database: process.env.DB_NAME,\n  user: process.env.DB_USER,\n  password: process.env.DB_PASSWORD,\n  max: 10,\n});',
   E'const pool = new Pool({\n  host: process.env.DB_HOST,\n  port: process.env.DB_PORT,\n  database: process.env.DB_NAME,\n  user: process.env.DB_USER,\n  password: process.env.DB_PASSWORD,\n  max: parseInt(process.env.MAX_DB_CONNECTIONS) || 50,\n  connectionTimeoutMillis: parseInt(process.env.DB_CONNECTION_TIMEOUT) || 5000,\n  idleTimeoutMillis: 30000,\n  maxUses: 7500,\n  allowExitOnIdle: false,\n});\n\npool.on(''error'', (err) => {\n  logger.error(''Database pool error:'', err);\n});',
   'acme-corp/catalog-service',
   'https://github.com/acme-corp/catalog-service/pull/152',
   152,
   'fix/connection-pool-leak',
   '2026-02-17 14:27:30');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Add connection pool utilization monitoring',
   'Expose pool.totalCount, pool.idleCount, and pool.waitingCount as Prometheus metrics. Create an alert that fires when pool utilization exceeds 80% to detect exhaustion before it causes outages.',
   'mitigation', 'safe', NULL,
   '2026-02-17 14:28:00');

  RAISE NOTICE 'Demo incident (Coroot) thoughts, citations, suggestions inserted.';
END $$;

-- Chat session (separate block for readability)
DO $$
DECLARE
  v_incident_id UUID := 'c0c00e01-db4c-4f8b-9a1d-2c5e8f3b6d91';
  v_chat_session_id VARCHAR(50) := 'c0c00e02-5e6f-7a8b-9c0d-1e2f3a4b5c6d';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
BEGIN

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: Database connectivity issues - catalog service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - COROOT INCIDENT\n\n## ALERT DETAILS:\n- **Title**: Database connectivity issues causing cascading latency - catalog service\n- **Status**: SLO Breached\n- **Source**: Coroot\n- **Incident**: kf0x7ljs\n- **SLO**: Latency compliance 98.96% (target: 99.9% < 500ms)\n- **Duration**: 24 minutes\n- **Affected Services**: catalog, front-end, order, db-main\n\n## CONNECTED PROVIDERS:\nYou have access to: coroot, github, github_repo_selection\n\n## GITHUB REPOSITORY CONTEXT:\n- **Connected Repository**: acme-corp/catalog-service\n- **Branch**: main\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis using the Coroot connector to query incidents, service maps, logs, traces, and metrics.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo-coroot-0001","toolCalls":[{"id":"tc_coroot_001","tool_name":"coroot_get_incident_detail","input":"{\"incident_key\": \"kf0x7ljs\", \"lookback_hours\": 1}","output":"{\"incident_key\":\"kf0x7ljs\",\"application\":\"front-end\",\"started\":\"2026-02-17T14:13:22Z\",\"resolved\":\"2026-02-17T14:37:23Z\",\"duration\":\"24m\",\"slos\":[{\"name\":\"Availability\",\"compliance\":\"100%\"},{\"name\":\"Latency\",\"objective\":\"99.9% < 500ms\",\"compliance\":\"98.96%\",\"breached\":true}],\"root_cause\":\"catalog service database connectivity issues with db-main\"}","status":"completed","timestamp":"2026-02-17T14:14:00.000000","command":"coroot_get_incident_detail: kf0x7ljs"},{"id":"tc_coroot_002","tool_name":"coroot_get_service_map","input":"{\"lookback_hours\": 1}","output":"{\"services\":[{\"name\":\"front-end\",\"status\":\"degraded\",\"dependencies\":[\"catalog\",\"order\"]},{\"name\":\"catalog\",\"status\":\"critical\",\"dependencies\":[\"db-main\"]},{\"name\":\"order\",\"status\":\"degraded\",\"dependencies\":[\"catalog\"]},{\"name\":\"db-main\",\"type\":\"PostgreSQL\",\"status\":\"warning\"}]}","status":"completed","timestamp":"2026-02-17T14:15:00.000000","command":"coroot_get_service_map"}]},
     {"sender":"bot","text":"The Coroot incident shows a latency SLO breach on the front-end service: 98.96% compliance vs the 99.9% target [1]. The service map reveals the dependency chain: **front-end → catalog → db-main** [2]. The catalog service is in CRITICAL state due to database connectivity failures. Let me drill into the catalog service details and logs.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo-coroot-0002","toolCalls":[{"id":"tc_coroot_003","tool_name":"coroot_get_app_detail","input":"{\"app_id\": \"tbuzvelk:default:Deployment:catalog\", \"lookback_hours\": 1}","output":"{\"app_id\":\"tbuzvelk:default:Deployment:catalog\",\"status\":\"CRITICAL\",\"error_rate\":\"10.4%\",\"p50_latency_ms\":245,\"p95_latency_ms\":850,\"p99_latency_ms\":1247,\"connections\":{\"active\":10,\"max\":10,\"waiting\":47},\"tcp_retransmissions\":\"elevated\",\"top_errors\":[\"operation was canceled\",\"context canceled\",\"connection pool exhausted\"]}","status":"completed","timestamp":"2026-02-17T14:16:00.000000","command":"coroot_get_app_detail: catalog"},{"id":"tc_coroot_004","tool_name":"coroot_get_app_logs","input":"{\"app_id\": \"tbuzvelk:default:Deployment:catalog\", \"severity\": \"Error\", \"lookback_hours\": 1, \"limit\": 50}","output":"[{\"timestamp\":\"2026-02-17T14:13:25Z\",\"severity\":\"Error\",\"message\":\"pq: canceling statement due to user request\",\"count\":347},{\"timestamp\":\"2026-02-17T14:15:41Z\",\"severity\":\"Error\",\"message\":\"connection pool exhausted: 0 idle, 10 active, 47 waiting\",\"count\":412},{\"timestamp\":\"2026-02-17T14:18:03Z\",\"severity\":\"Error\",\"message\":\"context canceled: connection to db-main timed out\",\"count\":488},{\"total_error_entries\":1247}]","status":"completed","timestamp":"2026-02-17T14:18:00.000000","command":"coroot_get_app_logs: catalog, severity=Error"}]},
     {"sender":"bot","text":"The catalog service is in bad shape [3]: 10.4% error rate, all 10 connections active with 47 requests waiting. Logs show 1,247 error entries: \"pq: canceling statement due to user request\" and \"connection pool exhausted\" [4]. The connection pool is fully saturated — **0 idle, 10 active, 47 waiting**. Let me check the error traces to understand which code path is leaking connections.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo-coroot-0003","toolCalls":[{"id":"tc_coroot_005","tool_name":"coroot_get_traces","input":"{\"service_name\": \"catalog\", \"status_error\": true, \"lookback_hours\": 1}","output":"{\"error_traces\":[{\"trace_id\":\"abc123def456\",\"service\":\"catalog\",\"operation\":\"GET /api/products/:id\",\"duration_ms\":5003,\"status\":\"ERROR\",\"error\":\"context canceled\",\"spans\":[{\"name\":\"pg.connect\",\"duration_ms\":5001,\"status\":\"ERROR\",\"attributes\":{\"pool.active\":10,\"pool.idle\":0}},{\"name\":\"product-lookup.js:lookupProduct\",\"duration_ms\":5003,\"status\":\"ERROR\"}]}],\"summary\":\"All error traces show connection acquisition timeout at pg.connect. Pool fully saturated (10/10 active, 0 idle).\"}","status":"completed","timestamp":"2026-02-17T14:20:00.000000","command":"coroot_get_traces: catalog, errors only"}]},
     {"sender":"bot","text":"Error traces confirm the issue [5]: every failed request is stuck at the `pg.connect` span waiting 5+ seconds to acquire a connection from the exhausted pool. The traces point to `product-lookup.js:lookupProduct` as the failing function. Connections are being acquired but **never released on error paths**. Let me search the code to find the exact bug.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo-coroot-0004","toolCalls":[{"id":"tc_coroot_006","tool_name":"mcp_search_code","input":"{\"q\": \"client.release repo:acme-corp/catalog-service\"}","output":"{\"results\":[{\"file\":\"catalog-service/src/handlers/product-lookup.js\",\"lines\":\"87-94\",\"content\":\"} catch (err) {\\n    console.error(''Product lookup failed:'', err);\\n    res.status(500).json({ error: ''Internal server error'' });\\n  }\",\"note\":\"Missing client.release() in catch block\"},{\"file\":\"catalog-service/src/db/connection.js\",\"lines\":\"42-58\",\"content\":\"const pool = new Pool({\\n  max: 10,\\n});\",\"note\":\"Pool max=10, no timeout config\"}]}","status":"completed","timestamp":"2026-02-17T14:22:00.000000","command":"GitHub: Search code \"client.release\" in catalog-service"},{"id":"tc_coroot_007","tool_name":"mcp_get_file_contents","input":"{\"owner\": \"acme-corp\", \"repo\": \"catalog-service\", \"path\": \"src/handlers/product-lookup.js\"}","output":"{\"file\":\"src/handlers/product-lookup.js\",\"content_excerpt\":\"async function lookupProduct(req, res) {\\n  const client = await pool.connect();\\n  try {\\n    const { productId } = req.params;\\n    const result = await client.query(\\n      ''SELECT * FROM products WHERE id = $1'',\\n      [productId]\\n    );\\n    res.json(result.rows[0]);\\n  } catch (err) {\\n    console.error(''Product lookup failed:'', err);\\n    res.status(500).json({ error: ''Internal server error'' });\\n  }\\n}\",\"analysis\":\"Line 87-94: catch block never releases the database connection. Every failed query leaks a connection permanently.\"}","status":"completed","timestamp":"2026-02-17T14:23:00.000000","command":"GitHub: Get src/handlers/product-lookup.js from acme-corp/catalog-service"}]},
     {"sender":"bot","text":"**Found the root cause.** Two code issues in the catalog-service repository:\n\n1. **`src/handlers/product-lookup.js:87-94`** [6, 7]: The `catch` block handles the error and sends a 500 response, but **never calls `client.release()`**. Every failed database query permanently leaks a connection from the pool.\n\n2. **`src/db/connection.js:42-58`** [6]: The pool is configured with `max: 10` and **no timeout settings** — no `connectionTimeoutMillis`, no `idleTimeoutMillis`, no `maxUses`. When connections leak, new requests wait indefinitely.\n\nUnder normal traffic (~200 req/s), the leak is slow enough to go unnoticed. But when the db-main replica had a 5-second network blip, multiple queries failed simultaneously, leaking all 10 connections at once and causing cascading failures.\n\nLet me apply an immediate fix and then prepare the permanent code change.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo-coroot-0005","toolCalls":[{"id":"tc_coroot_008","tool_name":"cloud_exec","input":"{\"command\": \"kubectl set env deployment/catalog-service MAX_DB_CONNECTIONS=50 DB_CONNECTION_TIMEOUT=5000 && kubectl rollout restart deployment/catalog-service\", \"provider\": \"aws\"}","output":"deployment.apps/catalog-service env updated\ndeployment.apps/catalog-service restarted\n\nRollout status: 3/3 replicas updated, available.\ncatalog-service error rate: 10.4% → 0.3% (recovered within 3 minutes)","status":"completed","timestamp":"2026-02-17T14:25:00.000000","command":"kubectl set env deployment/catalog-service MAX_DB_CONNECTIONS=50 DB_CONNECTION_TIMEOUT=5000 && kubectl rollout restart deployment/catalog-service"}]},
     {"sender":"bot","text":"Immediate fix applied [9]. Connection pool scaled from 10 to 50 with a 5-second timeout. Catalog service error rate has dropped from 10.4% to 0.3% — service health restored. Now preparing the permanent code fix.","message_number":7,"isCompleted":true,"run_id":"lc_run--demo-coroot-0006","toolCalls":[{"id":"tc_coroot_009","tool_name":"github_fix","input":"{\"repo\": \"acme-corp/catalog-service\", \"file_path\": \"src/handlers/product-lookup.js\", \"description\": \"Add try-finally to ensure client.release() on all code paths\"}","output":"Fix suggestion created: Replace catch-only error handling with try-catch-finally pattern. The finally block ensures client.release() is always called, preventing connection leaks.","status":"completed","timestamp":"2026-02-17T14:26:00.000000","command":"Suggest code fix"},{"id":"tc_coroot_010","tool_name":"github_apply_fix","input":"{\"repo\": \"acme-corp/catalog-service\", \"branch\": \"fix/connection-pool-leak\", \"title\": \"Fix: Add connection cleanup in product lookup error handling\"}","output":"Pull request created successfully.\n- Branch: fix/connection-pool-leak\n- PR #152: https://github.com/acme-corp/catalog-service/pull/152\n- Changes: src/handlers/product-lookup.js, src/db/connection.js","status":"completed","timestamp":"2026-02-17T14:27:00.000000","command":"Create pull request"}]},
     {"sender":"bot","text":"Investigation complete. Here is the full summary:\n\n**Root Cause**: Database connection leak in `catalog-service/src/handlers/product-lookup.js:87-94`. The `catch` block never calls `client.release()`, leaking connections on every failed query. Combined with a small pool (`max: 10`, no timeouts), a brief network partition to db-main exhausted all connections in 30 seconds, causing cascading 500 errors across catalog → front-end → order.\n\n**Actions Taken**:\n1. **Immediate**: Scaled connection pool 10 → 50 + added 5s timeout via `kubectl set env` [9]. Service recovered in 3 minutes.\n2. **Permanent**: Created PR #152 with two fixes [10]:\n   - `product-lookup.js`: Added `try-finally` block with `client.release()` \n   - `connection.js`: Increased pool max, added `connectionTimeoutMillis`, `idleTimeoutMillis`, `maxUses`\n\n**Recommended Follow-ups**:\n- Review and merge PR #152 after load testing in staging\n- Add Prometheus metrics for pool utilization (`pool.totalCount`, `pool.idleCount`, `pool.waitingCount`)\n- Alert when pool utilization exceeds 80% to catch exhaustion early\n- Audit other handlers in catalog-service for similar missing `release()` calls","message_number":8,"isCompleted":true,"run_id":"lc_run--demo-coroot-0007","toolCalls":[]}
   ]')::jsonb,
   '2026-02-17 14:13:22', '2026-02-17 14:28:00', true, 'completed', v_incident_id);

  -- Fix tool call IDs and run_ids for proper rendering
  UPDATE chat_sessions SET messages = (
    SELECT jsonb_agg(
      CASE
        WHEN msg->'toolCalls' IS NOT NULL AND jsonb_typeof(msg->'toolCalls') = 'array' AND jsonb_array_length(msg->'toolCalls') > 0 THEN
          jsonb_set(msg, '{toolCalls}', (
            SELECT jsonb_agg(
              CASE
                WHEN updated_tc->>'tool_name' = 'cloud_exec' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'command')
                WHEN updated_tc->>'tool_name' = 'github_fix' THEN
                  updated_tc || jsonb_build_object('command', 'Suggest code fix')
                WHEN updated_tc->>'tool_name' = 'github_apply_fix' THEN
                  updated_tc || jsonb_build_object('command', 'Create pull request')
                ELSE updated_tc
              END
            )
            FROM (
              SELECT jsonb_set(
                jsonb_set(tc, '{id}', to_jsonb('toolu_vrtx_' || replace(gen_random_uuid()::text, '-', ''))),
                '{run_id}', to_jsonb('lc_run--' || gen_random_uuid()::text)
              ) as updated_tc
              FROM jsonb_array_elements(msg->'toolCalls') as tc
            ) sub
          ))
        ELSE msg
      END
    )
    FROM jsonb_array_elements(messages) as msg
  )
  WHERE id = v_chat_session_id;

  RAISE NOTICE 'Demo incident (Coroot) fully inserted.';
END $$;

-- Ensure resolved status on existing installs
UPDATE incidents SET status = 'resolved' WHERE id = 'c0c00e01-db4c-4f8b-9a1d-2c5e8f3b6d91' AND status != 'resolved';

-- Postmortem for coroot incident (idempotent)
INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'c0c00e01-db4c-4f8b-9a1d-2c5e8f3b6d91',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_coroot$# Postmortem: Database connectivity issues causing cascading latency - catalog service

**Date:** 2026-02-17 14:13 UTC
**Duration:** 14m
**Severity:** critical
**Service:** catalog
**Source:** coroot

## Summary
The catalog service experienced database connectivity failures with PostgreSQL instance db-main due to connection pool exhaustion. The incident was triggered by missing `client.release()` calls in error handling paths combined with an undersized connection pool (max 10 connections). This caused cascading failures to front-end and order services, with p99 latency SLO compliance degrading to 98.96% against a 99.9% target.

## Timeline
- **14:13:30** - SLO breach detected on front-end service; latency compliance dropped to 98.96% against 99.9% target
- **14:14:30** - Coroot RCA identified database connectivity issues between catalog service and db-main PostgreSQL
- **14:15:00** - Service dependency chain confirmed: front-end → catalog → db-main; order service also affected
- **14:17:00** - Catalog service showing 10% error rate with "operation was canceled" and "context canceled" errors
- **14:19:00** - Logs revealed 1,247 occurrences of "pq: canceling statement due to user request" indicating connection pool exhaustion
- **14:21:00** - Traces identified product lookup endpoint (/api/products/:id) as primary failure point
- **14:23:00** - Root cause identified: connection leak in product-lookup.js:87-94 where catch block never calls client.release()
- **14:24:00** - Secondary issue found: pool configuration in connection.js:42-58 with max size of only 10 and no timeout settings
- **14:25:00** - Immediate mitigation applied: scaled connection pool from 10 to 50 and added 5s timeout via env var update and pod restart
- **14:26:00** - Catalog service recovering; error rate dropping
- **14:28:00** - Investigation complete; permanent fix submitted as PR #152

## Root Cause
The catalog service's database connection pool was exhausted due to two compounding issues:

1. **Connection leak in error handling**: The product lookup handler at `catalog-service/src/handlers/product-lookup.js:87-94` failed to call `client.release()` in the catch block, causing connections to leak on every failed query.

2. **Undersized and misconfigured connection pool**: The pool at `catalog-service/src/db/connection.js:42-58` was initialized with only 10 maximum connections and lacked timeout or lifecycle configuration.

When a PostgreSQL replica experienced a 5-second network partition at 14:12 UTC, several connections hung indefinitely. Combined with the connection leak, the pool exhausted all 10 connections within 30 seconds, causing all subsequent requests to fail.

## Impact
- **Catalog service**: Approximately 28,800 requests failed during the 24-minute incident window (200 req/s × 24 min × 10% error rate)
- **Front-end service**: p95/p99 latency increased from 180ms to 1,200ms as requests timed out waiting for catalog responses
- **Order service**: 15% of order placements failed due to missing inventory checks from catalog service
- **Customer impact**: Customer-facing errors increased by 12%
- **SLO breach**: Latency compliance degraded to 98.96% against 99.9% target for requests served under 500ms

## Resolution
The incident was resolved by scaling the database connection pool from 10 to 50 connections and adding a 5-second connection timeout via environment variable update and pod restart at 14:25 UTC. Service health was restored within 3 minutes. A permanent fix addressing the connection leak in error handling paths was submitted as PR #152.

## Action Items
- [ ] Merge and deploy PR #152 to fix connection leak in product-lookup.js error handling with proper try-finally cleanup
- [ ] Update database connection pool configuration in connection.js:42-58 to include connectionTimeoutMillis, idleTimeoutMillis, and maxUses settings
- [ ] Implement connection pool monitoring by exposing pool.totalCount, pool.idleCount, and pool.waitingCount as Prometheus metrics
- [ ] Create alert for connection pool utilization exceeding 80% to detect exhaustion before outages occur
- [ ] Conduct code review of all database query handlers to identify and fix similar connection leak patterns
- [ ] Document connection pool best practices and add to service development guidelines
- [ ] Review connection pool sizing across all services to ensure adequate capacity for expected load

## Lessons Learned
- **Resource cleanup is critical**: Missing `client.release()` calls in error paths can quickly exhaust connection pools under normal load. All database operations must use try-finally blocks to ensure resources are always released.
- **Connection pool sizing matters**: A pool size of 10 connections was insufficient for 200 req/s traffic. Connection pools should be sized based on expected concurrency with adequate headroom for bursts.
- **Configuration defaults are insufficient**: Connection pools require explicit timeout and lifecycle settings to prevent indefinite hangs and resource exhaustion.
- **Observability gaps delay detection**: Lack of connection pool metrics prevented early detection of pool exhaustion. Monitoring resource utilization is as important as monitoring error rates.
- **Cascading failures amplify impact**: A failure in a mid-tier service (catalog) cascaded to both upstream (front-end) and downstream (order) services, multiplying the customer impact. Circuit breakers and graceful degradation should be considered for critical dependency chains.$postmortem_coroot$,
  '2026-02-27 01:21:14',
  '2026-02-27 01:21:14'
)
ON CONFLICT (incident_id) DO NOTHING;
