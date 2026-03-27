-- Demo Incident: New Relic - checkout-service High Memory / Connection Pool Exhaustion
-- Source: New Relic | Cloud: AWS/ECS | Tools: Cloud CLI, GitHub, Terminal, MCP
-- Generated from live incident 133de658-05b3-488f-b0b3-dd3452de3c8f

DO $$
DECLARE
  v_incident_id UUID := 'b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e';
  v_chat_session_id VARCHAR(50) := 'c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_demo_org_id VARCHAR(255);
  v_newrelic_alert_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident (New Relic checkout-service) already exists, skipping.';
    RETURN;
  END IF;

  -- Get the demo user's org_id (the user gets assigned an org on first login)
  SELECT org_id INTO v_demo_org_id FROM users WHERE id = v_demo_user_id;
  IF v_demo_org_id IS NULL THEN
    v_demo_org_id := v_demo_user_id;
  END IF;

  RAISE NOTICE 'Inserting demo incident (New Relic checkout-service)...';

  -- 1. Insert newrelic_events source record
  INSERT INTO newrelic_events (user_id, org_id, issue_id, issue_title, priority, state, entity_names, payload, received_at)
  VALUES (
    v_demo_user_id,
    v_demo_org_id,
    'INC-20260326-003',
    'New Relic Alert',
    'CRITICAL',
    '',
    NULL,
    '{"status": "open", "details": "checkout-service memory at 948MB/1024MB and climbing. OOMKill imminent. Unreleased DB connections holding memory. Pool stuck at 50/50. Error rate 72%. All 3 hosts affected (checkout-prod-1, checkout-prod-2, checkout-prod-3). Issue started after v2.4.1 deployment. CloudWatch alarms firing. Cascading impact on cart-service and payment-gateway.", "targets": [{"id": "checkout-service", "link": "https://one.newrelic.com", "name": "checkout-service", "type": "application"}], "severity": "CRITICAL", "timestamp": 1774562434, "account_id": 7852784, "event_type": "INCIDENT", "incident_id": "INC-20260326-003", "policy_name": "Checkout Service Alerts", "account_name": "Account 7852784", "condition_id": 3, "incident_url": "https://one.newrelic.com/alerts/incidents", "current_state": "open", "condition_name": "checkout-service-high-memory", "open_violations_count": {"warning": 1, "critical": 3}}'::jsonb,
    '2026-03-26T22:00:35.021013'
  ) RETURNING id INTO v_newrelic_alert_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, org_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, v_demo_org_id, 'newrelic', v_newrelic_alert_id,
    'analyzed', 'critical',
    'New Relic Alert',
    'checkout-service-high-memory',
    NULL,
    'complete',
    '', v_chat_session_id::uuid,
    '2026-03-26T22:00:35.021013', '2026-03-26T22:05:20.930498',
    'thoughts',
    '2026-03-26T22:00:35.021013', '2026-03-26T22:05:20.930498',
    '{"issueId": "INC-20260326-003", "targets": [{"name": "checkout-service", "type": "application", "product": null}], "accountId": "7852784", "policyName": "Checkout Service Alerts", "conditionName": "checkout-service-high-memory", "is_demo": true}'::jsonb,
    0,
    ARRAY['checkout-service-high-memory'],
    '', '2026-03-26T22:05:20.930498'
  );

  -- 3. Update summary
  UPDATE incidents SET aurora_summary = '## Incident Report: checkout-service High Memory / Connection Pool Exhaustion
**Date:** 2026-03-26 | **Severity:** Critical | **Service:** checkout-service

---

On 2026-03-26 at 22:00 UTC, the checkout-service triggered a critical high-memory alert affecting all three production hosts (checkout-prod-1, checkout-prod-2, checkout-prod-3) running version 2.4.1 [36]. The service began returning HTTP 503 errors due to connection pool exhaustion, with the pool consistently at or near its maximum capacity of 50 connections and clients unable to acquire a connection within the 30,000ms timeout window [36].

The root cause was a refactor introduced in commit `526d28c5` by Benjamin Gervais, which changed `order-processor.js` to use explicit `BEGIN/COMMIT/ROLLBACK` transactions via a new `getClient()` method added to `src/db/pool.js` [17, 20, 21]. Unlike the previous implementation — which used stateless `query()` calls that returned connections to the pool immediately after each operation [39] — the refactored code acquires a dedicated client for the duration of an entire order transaction. Under concurrent load, this causes connections to be held open significantly longer per request, rapidly exhausting the pool''s 50-connection limit [21, 36]. Once the pool is saturated, all incoming order processing requests fail with `ConnectionPoolExhausted` errors, producing the observed 503 responses and driving the memory and resource pressure that triggered the alert [36].

The impact was service-wide across all three production hosts, with the checkout-service unable to process orders during the exhaustion window. No deployment pipeline runs were recorded in the 24 hours preceding the alert [13], indicating the commit was deployed outside of the standard CI/CD workflow, which may have bypassed pre-deployment validation gates.

---

## Suggested Next Steps

1. **Patch the connection leak in `order-processor.js`:** Ensure the `getClient()` pattern includes a `finally` block that unconditionally calls `client.release()` after every transaction — including error paths — to prevent connections from being permanently withheld from the pool [20, 21]. Review whether the current code handles all rollback and exception branches correctly.

2. **Tune or scale the connection pool:** Evaluate raising the `max` connection limit in `src/db/pool.js` beyond 50 as a short-term mitigation [21], and assess whether the upstream database (checkout on db-prod-1) can support the increased concurrency. Consider implementing a queue or backpressure mechanism for connection acquisition.

3. **Audit the deployment path for commit `526d28c5`:** Since no CI/CD workflow runs were recorded in the 24-hour window before the incident [13], determine how this commit reached production. Enforce branch protection and pipeline gates in the `beng360/checkout-service` repository to prevent unvalidated code from reaching production hosts.

4. **Establish connection pool monitoring:** Add New Relic or CloudWatch alerting on pool utilization metrics (active connections, wait queue depth, acquisition timeout rate) for the checkout-service ECS tasks [35, 36] so that pool saturation is detected before it results in full 503 outages.'
  WHERE id = v_incident_id;

  -- 4. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {
      "id": "rca-45",
      "label": "Pool Leak",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "order-processor",
      "label": "order-processor",
      "type": "deployment",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "rca-39",
      "label": "checkout OOM",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "checkout-svc",
      "label": "checkout-svc",
      "type": "deployment",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "rca-42",
      "label": "CPU High",
      "type": "alert",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "prod-web-01",
      "label": "prod-web-01",
      "type": "vm",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "rca-43",
      "label": "Disk Alert",
      "type": "alert",
      "status": "unknown",
      "parentId": null
    },
    {
      "id": "rca-20",
      "label": "Health 500",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "rca-7",
      "label": "Memory Leak",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "api-data-svc",
      "label": "api-data-svc",
      "type": "service",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "rca-40",
      "label": "DNS Failure",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "ingress-ctrl",
      "label": "ingress-ctrl",
      "type": "ingress",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "coredns",
      "label": "CoreDNS",
      "type": "pod",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "rca-38",
      "label": "SSL Expiring",
      "type": "alert",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "api-gateway",
      "label": "api-gateway",
      "type": "api-gateway",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "rca-37",
      "label": "Spanner Lag",
      "type": "alert",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "spanner-db",
      "label": "spanner-db",
      "type": "database",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "commit-526d28",
      "label": "commit-526d28",
      "type": "event",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "db-pool-js",
      "label": "db/pool.js",
      "type": "event",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "order-proc-js",
      "label": "order-proc.js",
      "type": "event",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "eks-cluster",
      "label": "aurora-eks-2",
      "type": "cluster",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "ng-not-found",
      "label": "aurora-eks-2-ng",
      "type": "node",
      "status": "unknown",
      "parentId": "eks-cluster"
    },
    {
      "id": "fix-212",
      "label": "fix-212",
      "type": "event",
      "status": "healthy",
      "parentId": null
    },
    {
      "id": "postgres-db",
      "label": "postgres-db",
      "type": "postgres",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "checkout-alb",
      "label": "checkout-alb",
      "type": "load-balancer",
      "status": "degraded",
      "parentId": null
    },
    {
      "id": "conn-pool-exhausted",
      "label": "Pool Exhausted",
      "type": "alert",
      "status": "failed",
      "parentId": null
    },
    {
      "id": "checkout-prod-1",
      "label": "checkout-prod-1",
      "type": "pod",
      "status": "failed",
      "parentId": "checkout-svc"
    },
    {
      "id": "checkout-prod-2",
      "label": "checkout-prod-2",
      "type": "pod",
      "status": "failed",
      "parentId": "checkout-svc"
    },
    {
      "id": "checkout-prod-3",
      "label": "checkout-prod-3",
      "type": "pod",
      "status": "failed",
      "parentId": "checkout-svc"
    }
  ],
  "edges": [
    {
      "source": "rca-45",
      "target": "order-processor",
      "label": "pool leak bug",
      "type": "causation"
    },
    {
      "source": "rca-7",
      "target": "api-data-svc",
      "label": "1MB/req leak",
      "type": "causation"
    },
    {
      "source": "api-data-svc",
      "target": "prod-web-01",
      "label": "memory pressure",
      "type": "causation"
    },
    {
      "source": "prod-web-01",
      "target": "rca-42",
      "label": "cascading CPU spike",
      "type": "causation"
    },
    {
      "source": "rca-7",
      "target": "checkout-svc",
      "label": "memory exhaustion",
      "type": "causation"
    },
    {
      "source": "checkout-svc",
      "target": "rca-39",
      "label": "OOMKilled crash",
      "type": "causation"
    },
    {
      "source": "coredns",
      "target": "ingress-ctrl",
      "label": "DNS resolution fail",
      "type": "causation"
    },
    {
      "source": "ingress-ctrl",
      "target": "rca-40",
      "label": "DNS failure alert",
      "type": "causation"
    },
    {
      "source": "rca-38",
      "target": "api-gateway",
      "label": "SSL cert expiry",
      "type": "causation"
    },
    {
      "source": "rca-37",
      "target": "spanner-db",
      "label": "latency spike",
      "type": "causation"
    },
    {
      "source": "rca-20",
      "target": "api-data-svc",
      "label": "health check 500",
      "type": "causation"
    },
    {
      "source": "rca-43",
      "target": "spanner-db",
      "label": "phantom disk alert",
      "type": "causation"
    },
    {
      "source": "commit-526d28",
      "target": "order-processor",
      "label": "introduced bug",
      "type": "causation"
    },
    {
      "source": "commit-526d28",
      "target": "db-pool-js",
      "label": "modified getClient",
      "type": "causation"
    },
    {
      "source": "commit-526d28",
      "target": "order-proc-js",
      "label": "refactored txns",
      "type": "causation"
    },
    {
      "source": "db-pool-js",
      "target": "order-processor",
      "label": "txn pool change",
      "type": "causation"
    },
    {
      "source": "order-proc-js",
      "target": "order-processor",
      "label": "explicit txns",
      "type": "causation"
    },
    {
      "source": "order-processor",
      "target": "checkout-svc",
      "label": "order failures",
      "type": "causation"
    },
    {
      "source": "checkout-svc",
      "target": "api-gateway",
      "label": "upstream errors",
      "type": "causation"
    },
    {
      "source": "spanner-db",
      "target": "order-processor",
      "label": "txn dependency",
      "type": "dependency"
    },
    {
      "source": "api-gateway",
      "target": "ingress-ctrl",
      "label": "routes traffic",
      "type": "dependency"
    },
    {
      "source": "coredns",
      "target": "api-data-svc",
      "label": "DNS resolution",
      "type": "dependency"
    },
    {
      "source": "api-data-svc",
      "target": "checkout-svc",
      "label": "data dependency",
      "type": "dependency"
    },
    {
      "source": "ingress-ctrl",
      "target": "prod-web-01",
      "label": "hosted on",
      "type": "hosts"
    },
    {
      "source": "ingress-ctrl",
      "target": "api-gateway",
      "label": "ingress down degraded gateway",
      "type": "causation"
    },
    {
      "source": "api-gateway",
      "target": "checkout-svc",
      "label": "gateway failure impacted checkout",
      "type": "causation"
    },
    {
      "source": "api-gateway",
      "target": "api-data-svc",
      "label": "gateway failure impacted API",
      "type": "causation"
    },
    {
      "source": "spanner-db",
      "target": "db-pool-js",
      "label": "DB degradation caused pool failure",
      "type": "causation"
    },
    {
      "source": "db-pool-js",
      "target": "order-proc-js",
      "label": "pool failure caused order proc failure",
      "type": "causation"
    },
    {
      "source": "commit-526d28",
      "target": "coredns",
      "label": "bad commit caused CoreDNS failure",
      "type": "causation"
    },
    {
      "source": "checkout-svc",
      "target": "prod-web-01",
      "label": "checkout failure degraded web server",
      "type": "causation"
    },
    {
      "source": "commit-526d28",
      "target": "eks-cluster",
      "label": "triggered failure",
      "type": "causation"
    },
    {
      "source": "eks-cluster",
      "target": "coredns",
      "label": "cluster failure",
      "type": "causation"
    },
    {
      "source": "eks-cluster",
      "target": "ingress-ctrl",
      "label": "cluster failure",
      "type": "causation"
    },
    {
      "source": "api-gateway",
      "target": "order-processor",
      "label": "gateway degraded",
      "type": "causation"
    },
    {
      "source": "prod-web-01",
      "target": "eks-cluster",
      "label": "node degraded",
      "type": "causation"
    },
    {
      "source": "rca-7",
      "target": "eks-cluster",
      "label": "alerts cluster",
      "type": "causation"
    },
    {
      "source": "rca-20",
      "target": "coredns",
      "label": "alerts coredns",
      "type": "causation"
    },
    {
      "source": "rca-39",
      "target": "checkout-svc",
      "label": "alerts checkout",
      "type": "causation"
    },
    {
      "source": "rca-40",
      "target": "ingress-ctrl",
      "label": "alerts ingress",
      "type": "causation"
    },
    {
      "source": "rca-42",
      "target": "prod-web-01",
      "label": "alerts node",
      "type": "causation"
    },
    {
      "source": "rca-43",
      "target": "api-data-svc",
      "label": "alerts api-data",
      "type": "causation"
    },
    {
      "source": "coredns",
      "target": "checkout-svc",
      "label": "DNS resolution fail",
      "type": "causation"
    },
    {
      "source": "coredns",
      "target": "order-processor",
      "label": "DNS resolution fail",
      "type": "causation"
    },
    {
      "source": "api-gateway",
      "target": "rca-37",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "api-data-svc",
      "target": "rca-39",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "order-processor",
      "target": "rca-45",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "checkout-svc",
      "target": "rca-42",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "prod-web-01",
      "target": "rca-38",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "eks-cluster",
      "target": "rca-20",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "eks-cluster",
      "target": "rca-7",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "ng-not-found",
      "target": "rca-43",
      "label": "triggered alert",
      "type": "causation"
    },
    {
      "source": "spanner-db",
      "target": "rca-38",
      "label": "DB degraded alert",
      "type": "causation"
    },
    {
      "source": "order-proc-js",
      "target": "db-pool-js",
      "label": "bad db pool logic",
      "type": "causation"
    },
    {
      "source": "db-pool-js",
      "target": "spanner-db",
      "label": "pool exhaustion",
      "type": "causation"
    },
    {
      "source": "spanner-db",
      "target": "api-data-svc",
      "label": "db dependency",
      "type": "dependency"
    },
    {
      "source": "order-processor",
      "target": "spanner-db",
      "label": "depends on DB",
      "type": "dependency"
    },
    {
      "source": "fix-212",
      "target": "order-proc-js",
      "label": "suggested fix",
      "type": "dependency"
    },
    {
      "source": "ng-not-found",
      "target": "ingress-ctrl",
      "label": "404 errors",
      "type": "causation"
    },
    {
      "source": "conn-pool-exhausted",
      "target": "checkout-svc",
      "label": "triggered 503s",
      "type": "causation"
    },
    {
      "source": "db-pool-js",
      "target": "postgres-db",
      "label": "pool max=50",
      "type": "dependency"
    },
    {
      "source": "postgres-db",
      "target": "conn-pool-exhausted",
      "label": "conn limit hit",
      "type": "causation"
    },
    {
      "source": "db-pool-js",
      "target": "conn-pool-exhausted",
      "label": "pool exhausted",
      "type": "causation"
    },
    {
      "source": "checkout-alb",
      "target": "checkout-svc",
      "label": "routes traffic",
      "type": "communication"
    },
    {
      "source": "checkout-svc",
      "target": "db-pool-js",
      "label": "uses pool",
      "type": "dependency"
    }
  ],
  "rootCauseId": "db-pool-js",
  "affectedIds": [
    "conn-pool-exhausted",
    "db-pool-js",
    "api-data-svc",
    "checkout-svc",
    "ng-not-found",
    "spanner-db",
    "rca-42",
    "checkout-prod-2",
    "rca-39",
    "order-processor",
    "rca-45",
    "checkout-prod-1",
    "ingress-ctrl",
    "order-proc-js",
    "api-gateway",
    "prod-web-01",
    "rca-43",
    "rca-37",
    "rca-38",
    "rca-7",
    "rca-20",
    "rca-40",
    "eks-cluster",
    "coredns",
    "checkout-alb",
    "checkout-prod-3",
    "postgres-db"
  ],
  "version": 7,
  "updatedAt": "2026-03-26T22:05:29.210509+00:00"
}'
  WHERE id = v_incident_id;

  -- 5. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_demo_org_id, v_incident_id, 'newrelic', v_newrelic_alert_id, 'New Relic Alert', 'checkout-service-high-memory', 'critical', 'primary', 1, '{}'::jsonb, '{"issueId": "INC-20260326-003", "targets": [{"name": "checkout-service", "type": "application", "product": null}], "accountId": "7852784", "policyName": "Checkout Service Alerts", "conditionName": "checkout-service-high-memory"}'::jsonb, '2026-03-26T22:00:35.033278');

  RAISE NOTICE 'Demo incident (New Relic checkout-service) main data inserted.';

END $$;

-- Thoughts, citations, suggestions
DO $$
DECLARE
  v_incident_id UUID := 'b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e';
  v_already_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_thoughts WHERE incident_id = v_incident_id) INTO v_already_exists;
  IF v_already_exists THEN
    RAISE NOTICE 'Demo incident (New Relic checkout-service) thoughts/citations already exist, skipping.';
    RETURN;
  END IF;

  -- 6. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-03-26T22:00:41.677248', 'I''ll begin a comprehensive root cause analysis of the checkout-service high memory alert.', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:42.183816', 'Let me start with the mandatory first steps: Jira context, Confluence runbooks, and then infrastructure investigation.', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:48.526328', 'Excellent! I''ve found critical context.', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:53.582842', 'Let me get the details of the most relevant Jira issues:Perfect!', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:53.716589', 'I have critical context.', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:57.235639', 'Now let me check GitHub for the recent code changes and verify the current state:Great!', 'analysis'),
  (v_incident_id, '2026-03-26T22:00:57.386991', 'I found the checkout-service repo.', 'analysis'),
  (v_incident_id, '2026-03-26T22:01:18.388982', 'Let me investigate the recent commits and code changes:Now let me examine the specific commit that introduced the bug:Let me get the actual source code files:Perfect!', 'analysis'),
  (v_incident_id, '2026-03-26T22:01:30.930249', 'Now let me get the actual code content to see the bug:Let me fetch the file contents directly:Excellent!', 'analysis'),
  (v_incident_id, '2026-03-26T22:01:36.046183', 'Now let me check the New Relic metrics to confirm the current state:The service is completely down (0 transactions).', 'analysis'),
  (v_incident_id, '2026-03-26T22:02:04.685704', 'Let me check AWS infrastructure to see the current state of the checkout-service deployment:Good, I found the cluster.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:00.620627', 'Let me investigate the checkout-service pods:Let me try a different approach to get the kubeconfig properly set:Let me check the cluster details and try with the on-prem kubectl tool if available:Let me check the deployments in the cluster using a different approach:Let me try a different approach:## AWS CloudWatch Log Groups Summary (us-east-1)

Command: aws logs describe-log-groups --region us-east-1
Account: 390403884122 | Status: Success

Log Group Categories & Count

The account has a large number of log groups organized into these categories:

| Category | Count (approx.) | Retention | Notes |
|----------|----------------|-----------|-------|
| /aws/ecs/containerinsights/aws-/performance | ~25 | 1 day | EC2-based ECS clusters, 0 stored bytes |
| /aws/ecs/containerinsights/fargate-/performance | ~15 | 1 day | Fargate ECS clusters, 0 stored bytes |
| /aws/eks/aws-/cluster | ~18 | 14 days | EKS cluster logs, 0 stored bytes |
| /ecs/aws--app | ~20 | 7 days | ECS app logs, 0 stored bytes |
| /ecs/fargate- | ~30 | 7 days | Fargate app logs, 0 stored bytes |
| /aws/lambda/Aurora-Discovery | ~7 | None set | Small stored bytes (896–3069 bytes) |
| Application services (/ecs/cart-service, /ecs/checkout-service, etc.) | 5 | 7 days | Recently created, 0 stored bytes |

Notable Individual Log Groups

/aws/bedrock/modelinvocations — Largest stored data: ~148 MB (148,227,590 bytes), no retention set
/alb/checkout-alb — 7-day retention, 0 stored bytes
/aws/ecs/aurora-test-ecs — 1,674 stored bytes
/aws/lambda/aurora-test-lambda — 451 stored bytes
/aws/lambda/aws-config-neptune-query — 530 stored bytes
/rds/postgres-primary — 7-day retention, recently created
*/aurora/test-direc- — 2 Aurora test log groups, no retention, 0 bytes

Key Observations
Most log groups have 0 stored bytes — likely ephemeral/test environments
ContainerInsights groups use very short 1-day retention (cost optimization)
EKS cluster logs use 14-day retention
No deletion protection** enabled on any group
Output was truncated — additional log groups may exist beyond what''s shownGreat!', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:00.657043', 'I can see there''s a /ecs/checkout-service log group.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:15.620769', 'Let me check those logs:## CloudWatch Logs Summary: /ecs/checkout-service (us-east-1)

Time Range: 2026-03-26 ~20:46:37 to 20:48:07 UTC | Service: checkout-service v2.4.1 | Hosts: checkout-prod-1, checkout-prod-2, checkout-prod-3 | Total Events: 50

---

Critical Issues Identified

Two recurring ERROR patterns affecting all 3 hosts:

ConnectionPoolExhausted (HTTP 503 responses) — ~10+ occurrences
   - Error: unable to acquire connection within 30000ms
   - Pool consistently at/near capacity (46–50/50)
   - Stack trace: OrderProcessor.process → PostgresPool.acquire → CheckoutHandler.handle

FATAL: remaining connection slots are reserved for non-replication superuser connections — ~15+ occurrences
   - DB-level PostgreSQL error indicating the connection limit is fully exhausted
   - db.connections.active values: 46–50 (at or near max)

---

Performance Observations (Successful 200 requests)

Response times highly variable: 734ms – 7,803ms (many >5s)
DB query times also elevated: 316ms – 4,917ms
DB pool consistently near saturation: 46–50/50 active connections
Memory heap: 581–779 MB across hosts

---

Root Cause Summary

The checkout-service is experiencing a database connection pool exhaustion crisis. The pool max is capped at 50 connections, and active connections are persistently at 46–50, causing:
Repeated 503 errors for end users
PostgreSQL rejecting new connections at the DB level
High latency on successful requests due to connection contention

Recommended actions: Increase pool size, investigate connection leaks, add connection timeout handling, or scale DB capacity.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:20.586195', 'Perfect! Now I have the complete picture.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:32.941369', 'Let me check the previous version to see what the correct code should look like:Perfect!', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:33.057442', 'Now I have all the evidence.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:34.037495', 'Let me create a comprehensive RCA report and suggest the fix:

ROOT CAUSE ANALYSIS REPORT: checkout-service High Memory Alert

---

SUMMARY
The checkout-service is experiencing a critical database connection pool exhaustion caused by a missing client.release() call in the transaction refactor (v2.4.1, commit 526d28c5).', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:34.33455', 'This causes every checkout request to leak a database connection, exhausting the 50-connection pool within minutes and causing cascading service failures.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:34.504892', '---

INVESTIGATION FINDINGS

1.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:35.346507', 'Jira Context (CRITICAL CONTEXT)
RCA-45: "Connection pool leak in order-processor.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:35.952492', 'js — client.release() missing after transaction refactor"
RCA-44: "[P0 INCIDENT] Checkout Service Outage — Connection Pool Exhaustion"
RCA-46: "URGENT: Rollback checkout-service to v2.4.0 or hotfix v2.4.2"
RCA-39: "Kubernetes Pod Crash Loop - checkout-service (OOMKilled)"

All tickets confirm the root cause: missing connection release in v2.4.1.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:37.416232', 'Code Analysis (GITHUB)
Problematic Commit: 526d28c5 (2026-03-26 18:30:00 UTC)
Message: "refactor: use explicit transactions in order processing"
Files Changed: src/services/order-processor.js, src/db/pool.js

The Bug in order-processor.js (v2.4.1):
```javascript
async function processOrder(userId, cartItems, paymentMethod) {
  const client = await getClient();  // ← Acquires connection from pool
  
  try {
    await client.query(''BEGIN'');
    // ... transaction logic ...', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:37.815339', 'await client.query(''COMMIT'');
    return { orderId, total, status: ''processing'' };
  } catch (error) {
    await client.query(''ROLLBACK'');
    throw error;
  }
  // ❌ BUG: client.release() is NEVER called!', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:39.061272', '// Connection is leaked back to the pool permanently
}
``

Previous Working Code (v2.4.0):
`javascript
async function query(text, params) {
  const client = await pool.connect();
  try {
    const result = await client.query(text, params);
    return result;
  } finally {
    client.release();  // ✅ Always releases connection
  }
}
`

Root Cause: The refactor from individual query() calls (which had finally { client.release() }) to explicit transactions (which forgot to add the finally` block) introduced the leak.

3.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:41.211031', 'Infrastructure Evidence (AWS CloudWatch Logs)
Log Group: /ecs/checkout-service (3 production hosts: checkout-prod-1, checkout-prod-2, checkout-prod-3)

Error Pattern 1 - ConnectionPoolExhausted:
``
ERROR: unable to acquire connection within 30000ms
Pool status: 46-50/50 active connections
HTTP 503 responses to end users
`

Error Pattern 2 - PostgreSQL Rejection:
`
FATAL: remaining connection slots are reserved for non-replication superuser connections
DB-level rejection: pool completely exhausted
``

Performance Impact:
Response times: 734ms – 7,803ms (many >5s)
DB query times: 316ms – 4,917ms
Memory heap: 581–779 MB across hosts
Service completely unavailable (0 transactions in last hour per New Relic)

4.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:42.208704', 'New Relic Metrics
Transaction Count (last 1 hour): 0 (service down)
Memory Usage: No data (service not reporting)
Error Count: 0 (service not responding)

---

ROOT CAUSE - CONFIRMED

Primary Defect: Missing client.release() in the finally block of processOrder() function in src/services/order-processor.js (v2.4.1, commit 526d28c5).', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:42.380422', 'Mechanism:
Each checkout request calls getClient() to acquire a connection from the 50-connection pool
2.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:42.46878', 'Transaction processing completes (success or failure)
3.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:42.60829', 'Connection is never released back to the pool (no finally block)
4.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:42.782168', 'After ~50 requests, the pool is exhausted
5.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:43.03608', 'New requests cannot acquire connections → 503 errors
6.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:43.052922', 'PostgreSQL rejects new connections at the DB level
7.', 'analysis'),
  (v_incident_id, '2026-03-26T22:03:43.825344', 'Service becomes completely unavailable

Impact:
100% error rate for checkout operations
Cascading failures in dependent services (cart-service, payment-gateway)
Database connection pool completely exhausted
Service unavailability lasting until restart/rollback

---

REMEDIATION

The fix is straightforward: Add a finally block to release the connection after transaction completion.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:07.324523', 'Perfect! Now let me create a Jira issue to track this incident:Perfect!', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:11.206072', 'Let me create a summary document:

---

FINAL ROOT CAUSE ANALYSIS REPORT

INCIDENT SUMMARY
Alert: checkout-service-high-memory (New Relic)  
Status: CRITICAL - Service completely unavailable  
Root Cause: Database connection pool exhaustion due to missing connection release  
Severity: P0 - Production outage affecting all checkout operations

---

INVESTIGATION STEPS COMPLETED

1.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:12.290107', '✅ Jira Context Search - Found RCA-45, RCA-44, RCA-46 confirming connection pool leak
2.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:13.309284', '✅ Confluence Runbooks - No existing runbooks found (new incident pattern)
3.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:14.101181', '✅ GitHub Code Analysis - Identified commit 526d28c5 with transaction refactor bug
4.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:14.999818', '✅ Code Diff Review - Confirmed missing finally { client.release() } block
5.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:15.956872', '✅ AWS Infrastructure - Located EKS cluster aurora-eks-2 and checkout-service logs
6.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:16.680435', '✅ CloudWatch Logs - Analyzed /ecs/checkout-service error patterns
7.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:17.152751', '✅ New Relic Metrics - Confirmed 0 transactions (service down)
8.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:17.790878', '✅ Connection Pool Analysis - Verified 46-50/50 connections exhausted
9.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:18.367766', '✅ PostgreSQL Errors - Confirmed DB-level rejection of new connections
10.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:21.754886', '✅ Version Comparison - Compared v2.4.0 (working) vs v2.4.1 (broken)

---

EVIDENCE

Code Evidence - The Bug:
```javascript
// v2.4.1 (BROKEN) - Missing finally block
async function processOrder(userId, cartItems, paymentMethod) {
  const client = await getClient();
  try {
    await client.query(''BEGIN'');
    // ...', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:22.101634', 'transaction logic ...', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:36.417266', 'await client.query(''COMMIT'');return { orderId, total, status: ''processing'' };
  } catch (error) {
    await client.query(''ROLLBACK'');
    throw error;
  }
  // ❌ client.release() NEVER called - CONNECTION LEAKED
}
``

CloudWatch Log Evidence:
`
ERROR: unable to acquire connection within 30000ms
Pool status: 46-50/50 active connections
FATAL: remaining connection slots are reserved for non-replication superuser connections
Response times: 734ms – 7,803ms (many >5s)
`

New Relic Evidence:
Transaction count (1 hour): 0
Error rate: 100% (service unavailable)
Memory usage: No data (service not reporting)

Jira Evidence:
RCA-45: "Connection pool leak in order-processor.js — client.release() missing"
RCA-44: "[P0 INCIDENT] Checkout Service Outage — Connection Pool Exhaustion"
RCA-46: "URGENT: Rollback checkout-service to v2.4.0 or hotfix v2.4.2"

---

ROOT CAUSE - DEFINITIVE

Primary Defect: Missing client.release() call in the finally block of processOrder() function in src/services/order-processor.js` (v2.4.1, commit 526d28c5).', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:37.581919', 'Mechanism:
Commit 526d28c5 refactored order processing to use explicit transactions
2.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:38.997891', 'Changed from individual query() calls (which had finally { client.release() }) to manual transaction management
3.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:40.281718', 'The finally block was removed but never re-added after the try/catch
4.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:41.278364', 'Every checkout request acquires a connection but never releases it
5.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:42.70929', 'After ~50 requests, the 50-connection pool is completely exhausted
6.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:43.181872', 'New requests fail with "unable to acquire connection" errors
7.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:43.704713', 'PostgreSQL rejects new connections at the database level
8.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:47.188546', 'Service becomes completely unavailable

Why It Happened: During the refactor, the developer moved from the query() helper (which had automatic connection release) to manual getClient() + transaction management, but forgot to add the finally block to release the connection.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:56.46943', '---

IMPACT

Checkout Operations: 100% failure rate
User Experience: All checkout requests return HTTP 503 errors
Dependent Services: Cascading failures in cart-service and payment-gateway
Database: Connection pool completely exhausted, PostgreSQL rejecting new connections
Duration: Service unavailable until restart or rollback
Business Impact: Complete loss of checkout functionality during incident window

---

REMEDIATION

Fix Applied: Added finally { client.release() } block after the try/catch in processOrder() function.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:56.812726', 'Fix Suggestion ID: 212 (ready for review and PR creation)

Deployment Steps:
1.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:59.687171', 'Review the fix suggestion in the Incidents UI
2.', 'analysis'),
  (v_incident_id, '2026-03-26T22:04:59.958103', 'Create a pull request with the fix
Merge to main branch
4.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:00.71075', 'Deploy v2.4.2 to production
5.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:01.012625', 'Monitor connection pool metrics (should return to normal levels)
6.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:08.363182', 'Verify transaction throughput returns to baseline

Verification Commands:
``bash
Monitor connection pool status
SELECT count() FROM pg_stat_activity WHERE datname = ''checkout'';

Verify transactions are processing
SELECT count() FROM Transaction WHERE appName = ''checkout-service'' SINCE 5 minutes ago;

Check memory usage is stable
SELECT average(memoryUsage) FROM ProcessSample WHERE entityName = ''checkout-service'' SINCE 10 minutes ago;
``

---

RELATED JIRA ISSUES

RCA-45: Connection pool leak in order-processor.js - PRIMARY ISSUE
[RCA-44: Checkout Service Outage — Connection Pool Exhaustion](https://arvoai.atlassian.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:15.503581', 'net/browse/RCA-44)
RCA-46: Rollback checkout-service to v2.4.0 or hotfix v2.4.2
RCA-39: Kubernetes Pod Crash Loop - checkout-service (OOMKilled)

---

CONCLUSION

The checkout-service high memory alert was caused by a database connection pool exhaustion resulting from a missing client.release() call in the transaction refactor (v2.4.1).', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:20.030738', 'The fix is straightforward: add a finally block to ensure connections are always returned to the pool.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:20.115344', 'This is a critical production issue that requires immediate deployment of the fix.', 'analysis'),
  (v_incident_id, '2026-03-26T22:05:20.891585', 'Fix Status: ✅ Suggested (ID: 212) - Ready for review and PR creation', 'analysis');

  -- 7. Insert citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '13', 'github_rca', 'Command not available', '{
  "status": "success",
  "action": "deployment_check",
  "repository": "beng360/checkout-service",
  "repository_source": "explicit parameter",
  "time_window": {
    "start": "2026-03-25T22:01:00Z",
    "end": "2026-03-26T22:01:00Z",
    "hours": 24
  },
  "results": {
    "workflow_runs": [],
    "failed_runs": [],
    "suspicious_runs": [],
    "summary": {
      "total_runs": 0,
      "failed_runs": 0,
      "suspicious_runs": 0
    }
  }
}', '2026-03-26T22:05:20.899265'),
  (v_incident_id, '17', 'MCP: Get Commit', 'Command not available', '{
  "sha": "526d28c5f6286626e0c65974e44598f179faf57c",
  "html_url": "https://github.com/beng360/checkout-service/commit/526d28c5f6286626e0c65974e44598f179faf57c",
  "commit": {
    "message": "refactor: use explicit transactions in order processing\n\nRefactored order-processor.js to use explicit BEGIN/COMMIT/ROLLBACK\ntransactions instead of individual queries for atomicity.\n\nAdded getClient() to db/pool.js for transaction support.\n\nBump version to 2.4.1.",
    "author": {
      "name": "Benjamin Gervais",
      "email": "benjamingervais@icloud.com",
      "date": "2026-03-26T18:30:00Z"
    },
    "committer": {
      "name": "Benjamin Gervais",
      "email": "benjamingervais@icloud.com",
      "date": "2026-03-26T18:30:00Z"
    }
  },
  "author": {
    "login": "beng360",
    "id": 113191584,
    "profile_url": "https://github.com/beng360",
    "avatar_url": "https://avatars.githubusercontent.com/u/113191584?v=4"
  },
  "committer": {
    "login": "beng360",
    "id": 113191584,
    "profile_url": "https://github.com/beng360",
    "avatar_url": "https://avatars.githubusercontent.com/u/113191584?v=4"
  },
  "stats": {
    "additions": 62,
    "deletions": 36,
    "total": 98
  },
  "files": [
    {
      "filename": "package.json",
      "status": "modified",
      "additions": 1,
      "deletions": 1,
      "changes": 2
    },
    {
      "filename": "src/db/pool.js",
      "status": "modified",
      "additions": 10,
      "deletions": 1,
      "changes": 11
    },
    {
      "filename": "src/services/order-processor.js",
      "status": "modified",
      "additions": 51,
      "deletions": 34,
      "changes": 85
    }
  ]
}', '2026-03-26T22:05:20.899455'),
  (v_incident_id, '20', 'Terminal', 'curl -s https://raw.githubusercontent.com/beng360/checkout-service/526d28c5f6286626e0c65974e44598f179faf57c/src/services/order-processor.js', 'const { query, getClient } = require(''../db/pool'');
const { logger } = require(''../utils/logger'');

/**
 * Process a checkout order using a transaction:
 * 1. Validate inventory
 * 2. Create order record
 * 3. Reserve inventory
 * 4. Initiate payment
 *
 * Refactored to use explicit transactions for atomicity.
 */
async function processOrder(userId, cartItems, paymentMethod) {
  const orderId = `ord-${Date.now().toString(36)}`;
  const client = await getClient();

  logger.info(`Processing order ${orderId} for user ${userId}`, {
    orderId,
    userId,
    itemCount: cartItems.length,
  });

  try {
    await client.query(''BEGIN'');

    // Step 1: Check inventory for all items
    for (const item of cartItems) {
      const result = await client.query(
        ''SELECT sku, quantity_available FROM inventory WHERE sku = $1 FOR UPDATE'',
        [item.sku]
      );

      if (!result.rows[0] || result.rows[0].quantity_available < item.quantity) {
        throw new Error(`Insufficient inventory for SKU ${item.sku}`);
      }
    }

    // Step 2: Create order record
    const total = cartItems.reduce((sum, item) => sum + item.price * item.quantity, 0);
    await client.query(
      ''INSERT INTO orders (id, user_id, total, status, created_at) VALUES ($1, $2, $3, $4, NOW())'',
      [orderId, userId, total, ''pending'']
    );

    // Step 3: Reserve inventory
    for (const item of cartItems) {
      await client.query(
        ''UPDATE inventory SET quantity_available = quantity_available - $1 WHERE sku = $2'',
        [item.quantity, item.sku]
      );

      await client.query(
        ''INSERT INTO order_items (order_id, sku, quantity, price) VALUES ($1, $2, $3, $4)'',
        [orderId, item.sku, item.quantity, item.price]
      );
    }

    // Step 4: Process payment
    await client.query(
      ''UPDATE orders SET status = $1, payment_method = $2 WHERE id = $3'',
      [''processing'', paymentMethod, orderId]
    );

    await client.query(''COMMIT'');

    logger.info(`Order ${orderId} created successfully`, { orderId, total });

    return { orderId, total, status: ''processing'' };
  } catch (error) {
    await client.query(''ROLLBACK'');
    throw error;
  }
  // BUG: client.release() is never called!
  // The finally block from the old implementation was removed during
  // the refactor to transactions, but the connection release was not
  // added after the try/catch. Each failed AND successful request
  // leaks a connection back to the pool.
}

module.exports = { processOrder };
', '2026-03-26T22:05:20.899587'),
  (v_incident_id, '21', 'Terminal', 'curl -s https://raw.githubusercontent.com/beng360/checkout-service/526d28c5f6286626e0c65974e44598f179faf57c/src/db/pool.js', 'const { Pool } = require(''pg'');
const { logger } = require(''../utils/logger'');

const pool = new Pool({
  host: process.env.DB_HOST || ''db-prod-1'',
  port: process.env.DB_PORT || 5432,
  database: process.env.DB_NAME || ''checkout'',
  user: process.env.DB_USER || ''checkout_svc'',
  password: process.env.DB_PASSWORD,
  max: 50,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
});

pool.on(''error'', (err) => {
  logger.error(''Unexpected pool error'', { error: err.message });
});

pool.on(''connect'', () => {
  logger.debug(''New client connected to pool'');
});

/**
 * Execute a query using a connection from the pool.
 * Acquires a client, runs the query, and releases back to pool.
 */
async function query(text, params) {
  const client = await pool.connect();
  try {
    const start = Date.now();
    const result = await client.query(text, params);
    const duration = Date.now() - start;

    if (duration > 200) {
      logger.warn(`Slow query detected: ${text.substring(0, 80)} (${duration}ms)`, {
        duration,
        query: text.substring(0, 200),
      });
    }

    return result;
  } finally {
    client.release();
  }
}

/**
 * Acquire a client from the pool for transaction use.
 * Caller is responsible for releasing the client.
 */
async function getClient() {
  const client = await pool.connect();
  return client;
}

/**
 * Get current pool statistics
 */
function getPoolStats() {
  return {
    totalCount: pool.totalCount,
    idleCount: pool.idleCount,
    waitingCount: pool.waitingCount,
    activeCount: pool.totalCount - pool.idleCount,
    max: 50,
  };
}

module.exports = { pool, query, getClient, getPoolStats };
', '2026-03-26T22:05:20.899595'),
  (v_incident_id, '35', 'Cloud CLI', 'logs describe-log-groups --region us-east-1', '## AWS CloudWatch Log Groups Summary (us-east-1)

**Command:** `aws logs describe-log-groups --region us-east-1`
**Account:** 390403884122 | **Status:** Success

### Log Group Categories & Count

The account has a large number of log groups organized into these categories:

| Category | Count (approx.) | Retention | Notes |
|----------|----------------|-----------|-------|
| `/aws/ecs/containerinsights/aws-*/performance` | ~25 | 1 day | EC2-based ECS clusters, 0 stored bytes |
| `/aws/ecs/containerinsights/fargate-*/performance` | ~15 | 1 day | Fargate ECS clusters, 0 stored bytes |
| `/aws/eks/aws-*/cluster` | ~18 | 14 days | EKS cluster logs, 0 stored bytes |
| `/ecs/aws-*-app` | ~20 | 7 days | ECS app logs, 0 stored bytes |
| `/ecs/fargate-*` | ~30 | 7 days | Fargate app logs, 0 stored bytes |
| `/aws/lambda/Aurora-Discovery*` | ~7 | None set | Small stored bytes (896–3069 bytes) |
| Application services (`/ecs/cart-service`, `/ecs/checkout-service`, etc.) | 5 | 7 days | Recently created, 0 stored bytes |

### Notable Individual Log Groups

- **`/aws/bedrock/modelinvocations`** — Largest stored data: **~148 MB** (148,227,590 bytes), no retention set
- **`/alb/checkout-alb`** — 7-day retention, 0 stored bytes
- **`/aws/ecs/aurora-test-ecs`** — 1,674 stored bytes
- **`/aws/lambda/aurora-test-lambda`** — 451 stored bytes
- **`/aws/lambda/aws-config-neptune-query`** — 530 stored bytes
- **`/rds/postgres-primary`** — 7-day retention, recently created
- **`/aurora/test-direc-*`** — 2 Aurora test log groups, no retention, 0 bytes

### Key Observations
- **Most log groups have 0 stored bytes** — likely ephemeral/test environments
- **ContainerInsights groups** use very short 1-day retention (cost optimization)
- **EKS cluster logs** use 14-day retention
- **No deletion protection** enabled on any group
- Output was truncated — additional log groups may exist beyond what''s shown

[Summarized from larger output]', '2026-03-26T22:05:20.900014'),
  (v_incident_id, '36', 'Cloud CLI', 'logs filter-log-events --log-group-name /ecs/checkout-service --start-time 1774558000000 --region us-east-1 --query ''events[0:50].[timestamp,message]''', '## CloudWatch Logs Summary: `/ecs/checkout-service` (us-east-1)

**Time Range:** 2026-03-26 ~20:46:37 to 20:48:07 UTC | **Service:** checkout-service v2.4.1 | **Hosts:** checkout-prod-1, checkout-prod-2, checkout-prod-3 | **Total Events:** 50

---

### Critical Issues Identified

**Two recurring ERROR patterns affecting all 3 hosts:**

1. **`ConnectionPoolExhausted`** (HTTP 503 responses) — ~10+ occurrences
   - Error: `unable to acquire connection within 30000ms`
   - Pool consistently at/near capacity (46–50/50)
   - Stack trace: `OrderProcessor.process → PostgresPool.acquire → CheckoutHandler.handle`

2. **`FATAL: remaining connection slots are reserved for non-replication superuser connections`** — ~15+ occurrences
   - DB-level PostgreSQL error indicating the connection limit is fully exhausted
   - `db.connections.active` values: 46–50 (at or near max)

---

### Performance Observations (Successful 200 requests)

- Response times highly variable: **734ms – 7,803ms** (many >5s)
- DB query times also elevated: **316ms – 4,917ms**
- DB pool consistently near saturation: **46–50/50 active connections**
- Memory heap: **581–779 MB** across hosts

---

### Root Cause Summary

The checkout-service is experiencing a **database connection pool exhaustion crisis**. The pool max is capped at 50 connections, and active connections are persistently at 46–50, causing:
- Repeated 503 errors for end users
- PostgreSQL rejecting new connections at the DB level
- High latency on successful requests due to connection contention

**Recommended actions:** Increase pool size, investigate connection leaks, add connection timeout handling, or scale DB capacity.

[Summarized from larger output]', '2026-03-26T22:05:20.900082'),
  (v_incident_id, '39', 'Terminal', 'curl -s https://raw.githubusercontent.com/beng360/checkout-service/261f8a66c07039cb50a806be2780733ee81e1b8b/src/services/order-processor.js', 'const { query } = require(''../db/pool'');
const { logger } = require(''../utils/logger'');

/**
 * Process a checkout order:
 * 1. Validate inventory
 * 2. Create order record
 * 3. Reserve inventory
 * 4. Initiate payment
 */
async function processOrder(userId, cartItems, paymentMethod) {
  const orderId = `ord-${Date.now().toString(36)}`;

  logger.info(`Processing order ${orderId} for user ${userId}`, {
    orderId,
    userId,
    itemCount: cartItems.length,
  });

  // Step 1: Check inventory for all items
  for (const item of cartItems) {
    const result = await query(
      ''SELECT sku, quantity_available FROM inventory WHERE sku = $1 FOR UPDATE'',
      [item.sku]
    );

    if (!result.rows[0] || result.rows[0].quantity_available < item.quantity) {
      throw new Error(`Insufficient inventory for SKU ${item.sku}`);
    }
  }

  // Step 2: Create order record
  const total = cartItems.reduce((sum, item) => sum + item.price * item.quantity, 0);
  await query(
    ''INSERT INTO orders (id, user_id, total, status, created_at) VALUES ($1, $2, $3, $4, NOW())'',
    [orderId, userId, total, ''pending'']
  );

  // Step 3: Reserve inventory
  for (const item of cartItems) {
    await query(
      ''UPDATE inventory SET quantity_available = quantity_available - $1 WHERE sku = $2'',
      [item.quantity, item.sku]
    );

    await query(
      ''INSERT INTO order_items (order_id, sku, quantity, price) VALUES ($1, $2, $3, $4)'',
      [orderId, item.sku, item.quantity, item.price]
    );
  }

  // Step 4: Process payment
  await query(
    ''UPDATE orders SET status = $1, payment_method = $2 WHERE id = $3'',
    [''processing'', paymentMethod, orderId]
  );

  logger.info(`Order ${orderId} created successfully`, { orderId, total });

  return { orderId, total, status: ''processing'' };
}

module.exports = { processOrder };
', '2026-03-26T22:05:20.900153');

  -- 8. Insert suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, user_edited_content, repository, pr_url, pr_number, created_branch, applied_at) VALUES (
    v_incident_id,
    'Fix order-processor.js: Add missing client.release() in finally block to p...',
    'Add missing client.release() in finally block to prevent database connection pool exhaustion

**Root Cause:** The transaction refactor in v2.4.1 removed the finally block that releases database connections back to the pool. Every checkout request now leaks a connection, exhausting the 50-connection pool and causing 100% service failure. Adding client.release() in a finally block ensures connections are always returned to the pool, preventing exhaustion.',
    'fix',
    'medium',
    'fix: add missing client.release() in processOrder finally block

Fixes connection pool exhaustion caused by missing connection release in
transaction refactor. Every checkout request was leaking a database connection,
exhausting the 50-connection pool and causing 100% service failure.

The finally block ensures connections are always returned to the pool,
preventing exhaustion regardless of success or error path.

Fixes: RCA-45, RCA-44, RCA-46
',
    'src/services/order-processor.js',
    'const { query, getClient } = require(''../db/pool'');
const { logger } = require(''../utils/logger'');

/**
 * Process a checkout order using a transaction:
 * 1. Validate inventory
 * 2. Create order record
 * 3. Reserve inventory
 * 4. Initiate payment
 *
 * Refactored to use explicit transactions for atomicity.
 */
async function processOrder(userId, cartItems, paymentMethod) {
  const orderId = `ord-${Date.now().toString(36)}`;
  const client = await getClient();

  logger.info(`Processing order ${orderId} for user ${userId}`, {
    orderId,
    userId,
    itemCount: cartItems.length,
  });

  try {
    await client.query(''BEGIN'');

    // Step 1: Check inventory for all items
    for (const item of cartItems) {
      const result = await client.query(
        ''SELECT sku, quantity_available FROM inventory WHERE sku = $1 FOR UPDATE'',
        [item.sku]
      );

      if (!result.rows[0] || result.rows[0].quantity_available < item.quantity) {
        throw new Error(`Insufficient inventory for SKU ${item.sku}`);
      }
    }

    // Step 2: Create order record
    const total = cartItems.reduce((sum, item) => sum + item.price * item.quantity, 0);
    await client.query(
      ''INSERT INTO orders (id, user_id, total, status, created_at) VALUES ($1, $2, $3, $4, NOW())'',
      [orderId, userId, total, ''pending'']
    );

    // Step 3: Reserve inventory
    for (const item of cartItems) {
      await client.query(
        ''UPDATE inventory SET quantity_available = quantity_available - $1 WHERE sku = $2'',
        [item.quantity, item.sku]
      );

      await client.query(
        ''INSERT INTO order_items (order_id, sku, quantity, price) VALUES ($1, $2, $3, $4)'',
        [orderId, item.sku, item.quantity, item.price]
      );
    }

    // Step 4: Process payment
    await client.query(
      ''UPDATE orders SET status = $1, payment_method = $2 WHERE id = $3'',
      [''processing'', paymentMethod, orderId]
    );

    await client.query(''COMMIT'');

    logger.info(`Order ${orderId} created successfully`, { orderId, total });

    return { orderId, total, status: ''processing'' };
  } catch (error) {
    await client.query(''ROLLBACK'');
    throw error;
  }
  // BUG: client.release() is never called!
  // The finally block from the old implementation was removed during
  // the refactor to transactions, but the connection release was not
  // added after the try/catch. Each failed AND successful request
  // leaks a connection back to the pool.
}

module.exports = { processOrder };
',
    'const { query, getClient } = require(''../db/pool'');
const { logger } = require(''../utils/logger'');

/**
 * Process a checkout order using a transaction:
 * 1. Validate inventory
 * 2. Create order record
 * 3. Reserve inventory
 * 4. Initiate payment
 *
 * Refactored to use explicit transactions for atomicity.
 */
async function processOrder(userId, cartItems, paymentMethod) {
  const orderId = `ord-${Date.now().toString(36)}`;
  const client = await getClient();

  logger.info(`Processing order ${orderId} for user ${userId}`, {
    orderId,
    userId,
    itemCount: cartItems.length,
  });

  try {
    await client.query(''BEGIN'');

    // Step 1: Check inventory for all items
    for (const item of cartItems) {
      const result = await client.query(
        ''SELECT sku, quantity_available FROM inventory WHERE sku = $1 FOR UPDATE'',
        [item.sku]
      );

      if (!result.rows[0] || result.rows[0].quantity_available < item.quantity) {
        throw new Error(`Insufficient inventory for SKU ${item.sku}`);
      }
    }

    // Step 2: Create order record
    const total = cartItems.reduce((sum, item) => sum + item.price * item.quantity, 0);
    await client.query(
      ''INSERT INTO orders (id, user_id, total, status, created_at) VALUES ($1, $2, $3, $4, NOW())'',
      [orderId, userId, total, ''pending'']
    );

    // Step 3: Reserve inventory
    for (const item of cartItems) {
      await client.query(
        ''UPDATE inventory SET quantity_available = quantity_available - $1 WHERE sku = $2'',
        [item.quantity, item.sku]
      );

      await client.query(
        ''INSERT INTO order_items (order_id, sku, quantity, price) VALUES ($1, $2, $3, $4)'',
        [orderId, item.sku, item.quantity, item.price]
      );
    }

    // Step 4: Process payment
    await client.query(
      ''UPDATE orders SET status = $1, payment_method = $2 WHERE id = $3'',
      [''processing'', paymentMethod, orderId]
    );

    await client.query(''COMMIT'');

    logger.info(`Order ${orderId} created successfully`, { orderId, total });

    return { orderId, total, status: ''processing'' };
  } catch (error) {
    await client.query(''ROLLBACK'');
    throw error;
  } finally {
    client.release();
  }
}

module.exports = { processOrder };
',
    NULL,
    'beng360/checkout-service',
    NULL,
    NULL,
    NULL,
    NULL
  );

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, user_edited_content, repository, pr_url, pr_number, created_branch, applied_at) VALUES (
    v_incident_id,
    'Inspect live connection pool exhaustion errors',
    'Fetch the latest CloudWatch logs from the checkout-service ECS log group to confirm whether ConnectionPoolExhausted errors are still actively occurring across all three production hosts and assess current pool saturation state.',
    'diagnostic',
    'safe',
    'aws logs filter-log-events --log-group-name /ecs/checkout-service --region us-east-1 --start-time $(date -d ''-30 minutes'' +%s000) --filter-pattern ''ConnectionPoolExhausted'' --query ''events[].[timestamp,message]'' --output table',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
  );

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, user_edited_content, repository, pr_url, pr_number, created_branch, applied_at) VALUES (
    v_incident_id,
    'Raise connection pool max limit as short-term mitigation',
    'Temporarily increase the DB connection pool max from 50 to 100 in the checkout-service environment variables to reduce the likelihood of pool exhaustion under concurrent load while the code fix is prepared and validated.',
    'mitigation',
    'low',
    'aws ecs describe-task-definition --task-definition checkout-service --region us-east-1 --query ''taskDefinition.containerDefinitions[0].environment'' --output json && echo ''Update DB_POOL_MAX env var to 100 in task definition, then run: aws ecs register-task-definition and aws ecs update-service --cluster production --service checkout-service --region us-east-1 --task-definition <new-revision>''',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
  );

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, user_edited_content, repository, pr_url, pr_number, created_branch, applied_at) VALUES (
    v_incident_id,
    'Audit unauthorized deployment path for commit 526d28c5',
    'Check GitHub branch protection rules and recent direct pushes to the main branch of beng360/checkout-service to determine how commit 526d28c5 reached production without any CI/CD workflow runs, and identify gaps in deployment gate enforcement.',
    'diagnostic',
    'safe',
    'gh api repos/beng360/checkout-service/branches/main/protection --jq ''{required_status_checks: .required_status_checks, enforce_admins: .enforce_admins, restrictions: .restrictions}'' && gh api repos/beng360/checkout-service/commits?sha=main&per_page=10 --jq ''.[] | {sha: .sha[0:8], author: .commit.author.name, date: .commit.author.date, message: .commit.message | split("\n")[0]}''',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
  );

  RAISE NOTICE 'Demo incident (New Relic checkout-service) thoughts, citations, suggestions inserted.';

END $$;