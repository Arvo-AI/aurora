-- Demo Incident 3: Periodic 502 errors from Redis Sentinel failover triggered by rogue CronJob
-- Source: Grafana | Cloud: AWS/EKS | Tools: kubectl, Splunk, GitHub, Knowledge Base, Web Search

DO $$
DECLARE
  v_incident_id UUID := 'd4e5f6a7-b8c9-4d0e-a1f2-3b4c5d6e7f80';
  v_chat_session_id VARCHAR(50) := 'e5f6a7b8-c9d0-4e1f-a2b3-4c5d6e7f8a90';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_grafana_alert_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident 3 already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident 3...';

  -- 1. Insert grafana_alerts source record
  INSERT INTO grafana_alerts (user_id, alert_uid, alert_title, alert_state, rule_name, rule_url, dashboard_url, panel_url, payload, received_at)
  VALUES (
    v_demo_user_id,
    'grafana-demo-003',
    'High 502 error rate on notification-service',
    'alerting',
    '502 Error Rate > 5%',
    'https://grafana.internal/alerting/grafana-demo-003/view',
    'https://grafana.internal/d/svc-overview/service-overview',
    'https://grafana.internal/d/svc-overview/service-overview?viewPanel=4',
    '{
      "title": "High 502 error rate on notification-service",
      "state": "alerting",
      "ruleUrl": "https://grafana.internal/alerting/grafana-demo-003/view",
      "message": "502 error rate for notification-service has exceeded 5% threshold. Current rate: 23%. Pattern appears periodic every 30 minutes.",
      "evalMatches": [{"metric": "http_error_rate_502", "value": 23.4, "tags": {"service": "notification-service", "namespace": "messaging"}}],
      "tags": {"env": "production", "service": "notification-service", "namespace": "messaging", "cluster": "eks-prod-us-west-2", "team": "platform-infra", "severity": "warning"}
    }'::jsonb,
    '2026-02-14 14:30:00'
  ) RETURNING id INTO v_grafana_alert_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'grafana', v_grafana_alert_id,
    'analyzed', 'medium',
    'High 502 error rate on notification-service',
    'notification-service', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-14 14:30:00', '2026-02-14 14:45:00',
    'thoughts', '2026-02-14 14:30:00', '2026-02-14 14:45:00',
    jsonb_build_object(
      'is_demo', true,
      'grafana_alert_uid', 'grafana-demo-003',
      'incidentId', 'grafana-demo-003',
      'source_url', 'https://grafana.internal/alerting/grafana-demo-003/view'
    ),
    3,
    ARRAY['notification-service', 'redis-sentinel', 'email-worker', 'sms-gateway'],
    '', '2026-02-14 14:45:00'
  );

  -- 3. Update summary
  UPDATE incidents SET aurora_summary = '# Incident Report: Periodic 502 Errors from Redis Sentinel Failover

## Summary

On February 14, 2026 at 14:30 UTC, Grafana alerting detected a recurring pattern of 502 errors on the notification-service in the EKS production cluster (eks-prod-us-west-2). The error rate spiked to 23% every 30 minutes in a perfectly periodic pattern, each burst lasting approximately 5 seconds before self-resolving [1, 2].

Investigation revealed the notification-service pods were healthy but losing their Redis connection during each spike [3, 4]. Splunk log analysis confirmed the 30-minute periodicity and showed Redis Sentinel triggering a failover event at the exact same cadence [5, 6]. The root cause was a Kubernetes CronJob named `redis-cleanup` deployed via PR #312 ("Add dev cache cleanup automation") three days earlier [9, 10]. The CronJob was intended for the dev environment but was misconfigured with the production Redis Sentinel address. Every 30 minutes it executed `redis-cli -h redis-sentinel.messaging.svc FLUSHDB`, which flushed the production Redis primary, causing Sentinel to detect the anomaly and initiate a failover. During the ~5 second failover window, all services depending on Redis received connection errors, manifesting as 502s at the load balancer.

The CronJob was immediately deleted to stop the recurring failures [12]. A code fix was also submitted to add Redis connection retry logic with exponential backoff to the notification-service, making it resilient to brief Sentinel failovers in the future.

## Suggested Next Steps

- **Audit all CronJobs for hardcoded production endpoints**: Search the cluster for CronJobs referencing production service addresses that may have been misconfigured.
- **Add namespace-scoped NetworkPolicies**: Prevent dev/staging workloads from reaching production Redis endpoints via Kubernetes NetworkPolicies.
- **Implement Redis connection resilience**: Add retry logic with exponential backoff to all services consuming Redis, so brief failovers do not cascade to end users.
- **Add CronJob deployment guardrails**: Require environment-specific validation in CI/CD pipelines before deploying CronJobs to production namespaces.'
  WHERE id = v_incident_id;

  -- 4. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "notification-svc", "label": "notification-service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "redis-sentinel", "label": "Redis Sentinel", "type": "database", "status": "degraded", "parentId": null},
    {"id": "redis-primary", "label": "Redis Primary", "type": "database", "status": "degraded", "parentId": "redis-sentinel"},
    {"id": "email-worker", "label": "email-worker", "type": "service", "status": "degraded", "parentId": null},
    {"id": "sms-gateway", "label": "sms-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "alb", "label": "ALB Ingress", "type": "service", "status": "degraded", "parentId": null},
    {"id": "cronjob", "label": "redis-cleanup CronJob", "type": "event", "status": "failed", "parentId": null},
    {"id": "pr-312", "label": "PR #312 Deploy", "type": "deployment", "status": "failed", "parentId": null},
    {"id": "alert-502", "label": "502 Error Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-redis", "label": "Redis Failover Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-latency", "label": "P99 Latency Alert", "type": "alert", "status": "failed", "parentId": null}
  ],
  "edges": [
    {"source": "pr-312", "target": "cronjob", "label": "deployed misconfigured CronJob", "type": "causation"},
    {"source": "cronjob", "target": "redis-primary", "label": "FLUSHDB every 30m", "type": "causation"},
    {"source": "redis-primary", "target": "redis-sentinel", "label": "triggers failover", "type": "causation"},
    {"source": "redis-sentinel", "target": "notification-svc", "label": "connection lost during failover", "type": "causation"},
    {"source": "redis-sentinel", "target": "email-worker", "label": "connection lost", "type": "causation"},
    {"source": "redis-sentinel", "target": "sms-gateway", "label": "connection lost", "type": "causation"},
    {"source": "notification-svc", "target": "alb", "label": "returns 502", "type": "causation"},
    {"source": "alb", "target": "alert-502", "label": "triggered alert", "type": "causation"},
    {"source": "redis-sentinel", "target": "alert-redis", "label": "triggered alert", "type": "causation"},
    {"source": "notification-svc", "target": "alert-latency", "label": "triggered alert", "type": "causation"}
  ],
  "rootCauseId": "pr-312",
  "affectedIds": ["cronjob", "redis-primary", "redis-sentinel", "notification-svc", "email-worker", "sms-gateway", "alb", "alert-502", "alert-redis", "alert-latency"],
  "version": 5,
  "updatedAt": "2026-02-14T14:45:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  -- 5. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id, 'High 502 error rate on notification-service', 'notification-service', 'warning', 'primary', 1.0, '{}', '{"tags":["env:production","service:notification-service"]}'::jsonb, '2026-02-14 14:30:00'),
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id + 1, 'Redis Sentinel failover detected in messaging namespace', 'redis-sentinel', 'warning', 'topology', 0.94, '{"reason":"Redis failover events coincide exactly with 502 spikes on dependent services"}'::jsonb, '{"tags":["env:production","service:redis-sentinel","namespace:messaging"]}'::jsonb, '2026-02-14 14:30:05'),
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id + 2, 'P99 latency spike on notification-service', 'notification-service', 'warning', 'time_proximity', 0.89, '{"reason":"Latency spike at identical timestamps as 502 errors"}'::jsonb, '{"tags":["env:production","service:notification-service","metric:latency"]}'::jsonb, '2026-02-14 14:30:10');

  RAISE NOTICE 'Demo incident 3 summary, viz, alerts inserted.';

END $$;

-- Thoughts, citations, suggestions
DO $$
DECLARE
  v_incident_id UUID := 'd4e5f6a7-b8c9-4d0e-a1f2-3b4c5d6e7f80';
BEGIN

  -- 6. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-14 14:30:30', 'Starting investigation of 502 errors on notification-service in the EKS production cluster (us-west-2).', 'analysis'),
  (v_incident_id, '2026-02-14 14:30:35', 'Searching knowledge base for Redis connection issues and 502 troubleshooting runbooks.', 'analysis'),
  (v_incident_id, '2026-02-14 14:30:40', 'Found a runbook for Redis Sentinel failover troubleshooting. It recommends checking Sentinel logs and pod connectivity.', 'analysis'),
  (v_incident_id, '2026-02-14 14:30:50', 'Checking pod status for notification-service. Need to determine if the pods themselves are crashing or if this is a dependency issue.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:00', 'All notification-service pods are Running with 0 restarts. This rules out application crashes -- the 502s must be from a transient dependency failure.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:10', 'Checking pod logs for connection errors. If Redis is the dependency, we should see connection refused or timeout errors.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:20', 'Logs show repeated "Redis connection lost" and "ECONNREFUSED" errors at regular intervals. The pattern is exactly every 30 minutes.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:30', 'A perfectly periodic failure every 30 minutes is not organic traffic. This looks like a scheduled job is causing the disruption.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:40', 'Querying Splunk for Redis Sentinel logs to see what happens at the exact failure timestamps.', 'analysis'),
  (v_incident_id, '2026-02-14 14:31:50', 'Splunk logs confirm Redis Sentinel is triggering a failover every 30 minutes. The Sentinel log shows "+switch-master" events at 14:00, 13:30, 13:00, 12:30, etc.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:00', 'Something is causing the Redis primary to become unavailable every 30 minutes, forcing Sentinel to elect a new primary. Let me check for CronJobs in the cluster.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:10', 'Found a suspicious CronJob named "redis-cleanup" in the messaging namespace. It runs every 30 minutes and executes redis-cli commands against the Sentinel endpoint.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:20', 'The CronJob runs: redis-cli -h redis-sentinel.messaging.svc FLUSHDB. This is flushing the production Redis primary database every 30 minutes.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:30', 'When FLUSHDB runs on the primary, Sentinel detects the anomaly and initiates a failover. During the ~5 second failover window, all clients lose their connections.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:40', 'Checking GitHub for who deployed this CronJob and when.', 'analysis'),
  (v_incident_id, '2026-02-14 14:32:50', 'Found it. PR #312 "Add dev cache cleanup automation" by dev.intern was merged 3 days ago. The PR description says "cleanup script for dev Redis" but the manifest points to the production Sentinel address.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:00', 'The diff clearly shows the CronJob hardcodes redis-sentinel.messaging.svc which is the production endpoint. It should have been redis.dev.svc or scoped to the dev namespace entirely.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:10', 'Root cause confirmed: a misconfigured CronJob deployed 3 days ago is executing FLUSHDB against production Redis every 30 minutes, triggering Sentinel failovers that cause 502s.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:20', 'Searching web for Redis Sentinel failover resilience best practices.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:30', 'Best practice is to implement connection retry with exponential backoff in Redis clients. The ioredis library supports sentinelRetryStrategy natively.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:40', 'Immediate fix: delete the rogue CronJob to stop the recurring failures.', 'analysis'),
  (v_incident_id, '2026-02-14 14:33:50', 'CronJob deleted successfully. Also generating a code fix to add Redis retry logic for long-term resilience.', 'analysis'),
  (v_incident_id, '2026-02-14 14:34:00', 'Creating PR #315 with ioredis Sentinel retry configuration and connection error handling.', 'analysis'),
  (v_incident_id, '2026-02-14 14:34:10', 'Investigation complete. Rogue CronJob removed, resilience fix submitted as PR #315.', 'analysis');

  -- 7. Insert citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Cloud CLI', 'kubectl get pods -n messaging -l app=notification-service -o wide', 'NAME                                    READY   STATUS    RESTARTS   AGE    IP            NODE
notification-svc-5d4c3b2a1-qw7e9      1/1     Running   0          18h    10.0.4.31     ip-10-0-4-31.us-west-2.compute.internal
notification-svc-5d4c3b2a1-rt8u2      1/1     Running   0          18h    10.0.5.17     ip-10-0-5-17.us-west-2.compute.internal
notification-svc-5d4c3b2a1-yp3k6      1/1     Running   0          18h    10.0.6.44     ip-10-0-6-44.us-west-2.compute.internal', '2026-02-14 14:30:55'),
  (v_incident_id, '2', 'Cloud CLI', 'kubectl get events -n messaging --sort-by=.lastTimestamp --field-selector reason!=Pulled,reason!=Created,reason!=Started | tail -20', 'LAST SEEN   TYPE      REASON              OBJECT                                    MESSAGE
2m          Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused
2m          Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-rt8u2    Readiness probe failed: Redis connection refused
2m          Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-yp3k6    Readiness probe failed: Redis connection refused
32m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused
32m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-rt8u2    Readiness probe failed: Redis connection refused
62m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused
62m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-rt8u2    Readiness probe failed: Redis connection refused', '2026-02-14 14:31:00'),
  (v_incident_id, '3', 'Cloud CLI', 'kubectl logs notification-svc-5d4c3b2a1-qw7e9 -n messaging --tail=25', '{"level":"info","message":"Notification sent to user_8f2a1b via email","timestamp":"2026-02-14T14:29:55.123Z"}
{"level":"info","message":"Processing notification queue batch: 24 items","timestamp":"2026-02-14T14:29:58.001Z"}
{"level":"error","message":"Redis connection lost: ECONNREFUSED 10.0.7.12:6379","timestamp":"2026-02-14T14:30:00.042Z"}
{"level":"error","message":"Redis connection lost: ECONNREFUSED 10.0.7.12:6379","timestamp":"2026-02-14T14:30:00.198Z"}
{"level":"error","message":"Failed to dequeue notification: Redis not available","timestamp":"2026-02-14T14:30:00.445Z"}
{"level":"warn","message":"Health check failed: cannot reach Redis","timestamp":"2026-02-14T14:30:01.002Z"}
{"level":"info","message":"Redis reconnected to new primary 10.0.7.15:6379","timestamp":"2026-02-14T14:30:05.331Z"}
{"level":"info","message":"Resuming notification processing","timestamp":"2026-02-14T14:30:05.502Z"}
{"level":"info","message":"Notification sent to user_3c7d9e via sms","timestamp":"2026-02-14T14:30:06.112Z"}', '2026-02-14 14:31:15'),
  (v_incident_id, '4', 'Cloud CLI', 'kubectl logs notification-svc-5d4c3b2a1-qw7e9 -n messaging --since=3h | grep -c "Redis connection lost"', '18', '2026-02-14 14:31:20'),
  (v_incident_id, '5', 'Splunk Search', 'index=kubernetes namespace=messaging sourcetype=redis-sentinel "switch-master" | timechart span=5m count', 'Time                 count
2026-02-14T12:00:00  1
2026-02-14T12:30:00  1
2026-02-14T13:00:00  1
2026-02-14T13:30:00  1
2026-02-14T14:00:00  1
2026-02-14T14:30:00  1

Pattern: Exactly 1 failover event every 30 minutes for the last 3 days (since 2026-02-11).', '2026-02-14 14:31:50'),
  (v_incident_id, '6', 'Splunk Search', 'index=kubernetes namespace=messaging sourcetype=redis-sentinel "+switch-master" | head 5', '+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T14:30:00.501Z]
+switch-master mymaster 10.0.7.15 6379 10.0.7.12 6379 [2026-02-14T14:00:00.498Z]
+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T13:30:00.503Z]
+switch-master mymaster 10.0.7.15 6379 10.0.7.12 6379 [2026-02-14T13:00:00.497Z]
+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T12:30:00.501Z]

Note: The primary alternates between .12 and .15 every 30 minutes -- classic forced failover pattern.', '2026-02-14 14:32:00');

  -- More citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '7', 'Cloud CLI', 'kubectl get cronjobs -n messaging', 'NAME              SCHEDULE       SUSPEND   ACTIVE   LAST SCHEDULE   AGE
redis-cleanup     */30 * * * *   False     0        2m              3d
queue-metrics     */5 * * * *    False     0        1m              45d', '2026-02-14 14:32:10'),
  (v_incident_id, '8', 'Cloud CLI', 'kubectl get cronjob redis-cleanup -n messaging -o yaml', 'apiVersion: batch/v1
kind: CronJob
metadata:
  name: redis-cleanup
  namespace: messaging
  labels:
    app: redis-cleanup
    created-by: dev.intern
spec:
  schedule: "*/30 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: redis-cleanup
            image: redis:7-alpine
            command:
            - /bin/sh
            - -c
            - redis-cli -h redis-sentinel.messaging.svc FLUSHDB
          restartPolicy: OnFailure', '2026-02-14 14:32:20'),
  (v_incident_id, '9', 'MCP: GitHub', 'github_rca: recent commits on k8s manifests', '{
  "commits": [
    {"sha": "f7a2c91", "message": "Merge PR #312: Add dev cache cleanup automation", "author": "dev.intern", "date": "2026-02-11T09:15:00Z"},
    {"sha": "b3e8d12", "message": "Update notification-service replica count to 3", "author": "sarah.ops", "date": "2026-02-10T16:45:00Z"},
    {"sha": "a1c5f67", "message": "Add Redis Sentinel monitoring dashboard", "author": "mike.sre", "date": "2026-02-09T11:20:00Z"}
  ],
  "timeline_correlation": "Commit f7a2c91 (PR #312) deployed exactly 3 days before issue start. CRITICAL correlation."
}', '2026-02-14 14:32:45'),
  (v_incident_id, '10', 'MCP: GitHub', 'github_rca: diff for PR #312', '{
  "status": "success",
  "action": "diff",
  "files_changed": 1,
  "additions": 28,
  "deletions": 0,
  "key_changes": [
    {
      "file": "k8s/messaging/cronjobs/redis-cleanup.yaml",
      "diff": "+ apiVersion: batch/v1\n+ kind: CronJob\n+ metadata:\n+   name: redis-cleanup\n+   namespace: messaging\n+ spec:\n+   schedule: \"*/30 * * * *\"\n+   jobTemplate:\n+     spec:\n+       template:\n+         spec:\n+           containers:\n+           - name: redis-cleanup\n+             image: redis:7-alpine\n+             command:\n+             - /bin/sh\n+             - -c\n+             - redis-cli -h redis-sentinel.messaging.svc FLUSHDB"
    }
  ],
  "pr_description": "Adds automated cleanup for dev Redis cache. Runs every 30 minutes to keep dev environment clean.",
  "reviewers": [],
  "review_comments": "No reviews were requested for this PR."
}', '2026-02-14 14:33:00'),
  (v_incident_id, '11', 'Web Search', 'Redis Sentinel failover resilience ioredis retry strategy best practices', 'Search results summary:

1. **Redis Sentinel Failover Handling** (redis.io/docs): "Client libraries should implement retry logic during Sentinel failovers. The failover process typically takes 3-5 seconds during which connections will be refused."

2. **ioredis Sentinel Configuration** (github.com/redis/ioredis): "Use sentinelRetryStrategy and retryStrategy options to handle failover gracefully. The enableOfflineQueue option buffers commands during disconnection."

3. **Kubernetes CronJob Best Practices** (kubernetes.io): "Always use namespace-scoped service references and validate target environments in CI/CD. Accidental cross-environment access is a common misconfiguration."', '2026-02-14 14:33:30'),
  (v_incident_id, '12', 'Cloud CLI', 'kubectl delete cronjob redis-cleanup -n messaging', 'cronjob.batch "redis-cleanup" deleted', '2026-02-14 14:33:45'),
  (v_incident_id, '13', 'Knowledge Base', 'Search: Redis Sentinel failover connection handling Kubernetes', 'Found: "Redis High Availability Runbook"

Key steps:
1. Check Sentinel logs for +switch-master events
2. Verify client retry configuration (sentinelRetryStrategy)
3. Check for external processes forcing failover (FLUSHDB, DEBUG SLEEP, SHUTDOWN)
4. Validate Sentinel quorum and down-after-milliseconds settings
5. Ensure all clients use Sentinel-aware connection libraries

Tags: redis, sentinel, failover, high-availability, kubernetes', '2026-02-14 14:30:40');

  -- 8. Insert suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Audit CronJobs for production endpoint references', 'Search all CronJobs across namespaces for hardcoded production service addresses. Any CronJob referencing production Redis, databases, or APIs from non-production namespaces should be flagged and corrected.', 'diagnostic', 'safe', 'kubectl get cronjobs -A -o json | jq -r ''.items[] | select(.spec.jobTemplate.spec.template.spec.containers[].command[]? | test("sentinel|redis-prod|messaging.svc")) | .metadata.namespace + "/" + .metadata.name''', '2026-02-14 14:34:00'),
  (v_incident_id, 'Verify Redis Sentinel health post-cleanup', 'After removing the rogue CronJob, verify Redis Sentinel is healthy and no more forced failovers occur. Monitor for 1 hour to confirm stability.', 'diagnostic', 'safe', 'kubectl exec -n messaging redis-sentinel-0 -- redis-cli -p 26379 SENTINEL masters', '2026-02-14 14:34:05'),
  (v_incident_id, 'Delete the rogue redis-cleanup CronJob', 'Immediately delete the misconfigured CronJob that is executing FLUSHDB against production Redis every 30 minutes, causing Sentinel failovers and 502 errors.', 'mitigation', 'low', 'kubectl delete cronjob redis-cleanup -n messaging', '2026-02-14 14:34:10');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Fix notification-service: Add Redis Sentinel retry logic with exponential backoff',
   'The notification-service crashes on Redis connection errors because it has no retry logic. Add ioredis Sentinel-aware retry configuration so brief failovers are handled transparently without returning 502s to users.',
   'fix', 'low', NULL,
   'services/notification-service/src/lib/redis.ts',
   E'import Redis from ''ioredis'';\n\nconst redis = new Redis({\n  host: process.env.REDIS_HOST || ''redis-sentinel.messaging.svc'',\n  port: 6379,\n});\n\nexport default redis;',
   E'import Redis from ''ioredis'';\n\nconst redis = new Redis({\n  sentinels: [\n    { host: process.env.REDIS_SENTINEL_HOST || ''redis-sentinel.messaging.svc'', port: 26379 },\n  ],\n  name: ''mymaster'',\n  retryStrategy(times: number) {\n    const delay = Math.min(times * 200, 5000);\n    return delay;\n  },\n  sentinelRetryStrategy(times: number) {\n    const delay = Math.min(times * 100, 3000);\n    return delay;\n  },\n  enableOfflineQueue: true,\n  maxRetriesPerRequest: 3,\n});\n\nredis.on(''error'', (err) => {\n  console.error(''Redis connection error:'', err.message);\n});\n\nredis.on(''reconnecting'', () => {\n  console.info(''Redis reconnecting to Sentinel...'');\n});\n\nexport default redis;',
   'acme-corp/platform',
   'https://github.com/acme-corp/platform/pull/315',
   315,
   'fix/aurora-redis-sentinel-retry',
   '2026-02-14 14:34:20');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Add NetworkPolicy to isolate production Redis', 'Create a Kubernetes NetworkPolicy that only allows pods in the messaging namespace to access the Redis Sentinel service. This prevents accidental cross-namespace access from dev or staging workloads.', 'mitigation', 'safe', NULL, '2026-02-14 14:34:30');

END $$;

-- 9. Chat session (separate block for readability)
DO $$
DECLARE
  v_incident_id UUID := 'd4e5f6a7-b8c9-4d0e-a1f2-3b4c5d6e7f80';
  v_chat_session_id VARCHAR(50) := 'e5f6a7b8-c9d0-4e1f-a2b3-4c5d6e7f8a90';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
BEGIN

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: High 502 error rate on notification-service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - GRAFANA ALERT\n\n## ALERT DETAILS:\n- **Title**: High 502 error rate on notification-service\n- **Status**: alerting\n- **Source**: grafana\n- **Rule**: 502 Error Rate > 5% (current: 23%)\n- **Pattern**: Periodic every 30 minutes\n\n## CONNECTED PROVIDERS:\nYou have access to: aws, grafana, github, github_repo_selection, splunk\n\n## GITHUB REPOSITORY CONTEXT:\n- **Connected Repository**: acme-corp/platform\n- **Branch**: main\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis. Use all available tools to investigate the periodic 502 pattern.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo3-0001","toolCalls":[{"id":"tc_demo3_001","tool_name":"knowledge_base_search","input":"{\"query\": \"Redis Sentinel failover connection handling Kubernetes 502\", \"limit\": 5}","output":"Found: Redis High Availability Runbook. Key steps: Check Sentinel logs for +switch-master events, verify client retry configuration, check for external processes forcing failover.","status":"completed","timestamp":"2026-02-14T14:30:40.000000"},{"id":"tc_demo3_002","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get pods -n messaging -l app=notification-service -o wide\", \"provider\": \"aws\"}","output":"NAME                                    READY   STATUS    RESTARTS   AGE    IP            NODE\nnotification-svc-5d4c3b2a1-qw7e9      1/1     Running   0          18h    10.0.4.31     ip-10-0-4-31.us-west-2.compute.internal\nnotification-svc-5d4c3b2a1-rt8u2      1/1     Running   0          18h    10.0.5.17     ip-10-0-5-17.us-west-2.compute.internal\nnotification-svc-5d4c3b2a1-yp3k6      1/1     Running   0          18h    10.0.6.44     ip-10-0-6-44.us-west-2.compute.internal","status":"completed","timestamp":"2026-02-14T14:30:55.000000"},{"id":"tc_demo3_003","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get events -n messaging --sort-by=.lastTimestamp --field-selector reason!=Pulled,reason!=Created,reason!=Started | tail -20\", \"provider\": \"aws\"}","output":"LAST SEEN   TYPE      REASON              OBJECT                                    MESSAGE\n2m          Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused\n2m          Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-rt8u2    Readiness probe failed: Redis connection refused\n32m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused\n62m         Warning   Unhealthy           pod/notification-svc-5d4c3b2a1-qw7e9    Readiness probe failed: Redis connection refused","status":"completed","timestamp":"2026-02-14T14:31:00.000000"}]},
     {"sender":"bot","text":"Found a Redis HA runbook [13]. All notification-service pods are Running with zero restarts [1] -- so this is not an application crash. However, Kubernetes events show repeated readiness probe failures with \"Redis connection refused\" at exactly 30-minute intervals [2]. Let me check the pod logs for the connection error pattern.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo3-0002","toolCalls":[{"id":"tc_demo3_004","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs notification-svc-5d4c3b2a1-qw7e9 -n messaging --tail=25\", \"provider\": \"aws\"}","output":"{\"level\":\"info\",\"message\":\"Notification sent to user_8f2a1b via email\",\"timestamp\":\"2026-02-14T14:29:55.123Z\"}\n{\"level\":\"error\",\"message\":\"Redis connection lost: ECONNREFUSED 10.0.7.12:6379\",\"timestamp\":\"2026-02-14T14:30:00.042Z\"}\n{\"level\":\"error\",\"message\":\"Failed to dequeue notification: Redis not available\",\"timestamp\":\"2026-02-14T14:30:00.445Z\"}\n{\"level\":\"warn\",\"message\":\"Health check failed: cannot reach Redis\",\"timestamp\":\"2026-02-14T14:30:01.002Z\"}\n{\"level\":\"info\",\"message\":\"Redis reconnected to new primary 10.0.7.15:6379\",\"timestamp\":\"2026-02-14T14:30:05.331Z\"}\n{\"level\":\"info\",\"message\":\"Resuming notification processing\",\"timestamp\":\"2026-02-14T14:30:05.502Z\"}","status":"completed","timestamp":"2026-02-14T14:31:15.000000"},{"id":"tc_demo3_005","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs notification-svc-5d4c3b2a1-qw7e9 -n messaging --since=3h | grep -c \\\"Redis connection lost\\\"\", \"provider\": \"aws\"}","output":"18","status":"completed","timestamp":"2026-02-14T14:31:20.000000"}]},
     {"sender":"bot","text":"Logs confirm the pattern: Redis connection is lost at :00/:30 of each hour, the service reconnects to a *different* Redis primary ~5 seconds later [3]. There have been 18 Redis disconnections in the last 3 hours [4]. The reconnection to a different IP (10.0.7.12 to 10.0.7.15) tells me Redis Sentinel is performing a failover. Let me query Splunk for the Sentinel logs.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo3-0003","toolCalls":[{"id":"tc_demo3_006","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=messaging sourcetype=redis-sentinel \\\"switch-master\\\" | timechart span=5m count\"}","output":"Time                 count\n2026-02-14T12:00:00  1\n2026-02-14T12:30:00  1\n2026-02-14T13:00:00  1\n2026-02-14T13:30:00  1\n2026-02-14T14:00:00  1\n2026-02-14T14:30:00  1\n\nPattern: Exactly 1 failover event every 30 minutes for the last 3 days.","status":"completed","timestamp":"2026-02-14T14:31:50.000000"},{"id":"tc_demo3_007","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=messaging sourcetype=redis-sentinel \\\"+switch-master\\\" | head 5\"}","output":"+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T14:30:00.501Z]\n+switch-master mymaster 10.0.7.15 6379 10.0.7.12 6379 [2026-02-14T14:00:00.498Z]\n+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T13:30:00.503Z]\n+switch-master mymaster 10.0.7.15 6379 10.0.7.12 6379 [2026-02-14T13:00:00.497Z]\n+switch-master mymaster 10.0.7.12 6379 10.0.7.15 6379 [2026-02-14T12:30:00.501Z]","status":"completed","timestamp":"2026-02-14T14:32:00.000000"}]},
     {"sender":"bot","text":"Splunk confirms it: Redis Sentinel has been executing a failover *exactly* every 30 minutes for the last 3 days [5]. The primary alternates between .12 and .15 each time [6]. This is not natural -- something is forcing the failover on a schedule. A 30-minute cron schedule is the most likely cause. Let me check for CronJobs in the cluster.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo3-0004","toolCalls":[{"id":"tc_demo3_008","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get cronjobs -n messaging\", \"provider\": \"aws\"}","output":"NAME              SCHEDULE       SUSPEND   ACTIVE   LAST SCHEDULE   AGE\nredis-cleanup     */30 * * * *   False     0        2m              3d\nqueue-metrics     */5 * * * *    False     0        1m              45d","status":"completed","timestamp":"2026-02-14T14:32:10.000000"},{"id":"tc_demo3_009","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get cronjob redis-cleanup -n messaging -o yaml\", \"provider\": \"aws\"}","output":"apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: redis-cleanup\n  namespace: messaging\n  labels:\n    app: redis-cleanup\n    created-by: dev.intern\nspec:\n  schedule: \"*/30 * * * *\"\n  jobTemplate:\n    spec:\n      template:\n        spec:\n          containers:\n          - name: redis-cleanup\n            image: redis:7-alpine\n            command:\n            - /bin/sh\n            - -c\n            - redis-cli -h redis-sentinel.messaging.svc FLUSHDB\n          restartPolicy: OnFailure","status":"completed","timestamp":"2026-02-14T14:32:20.000000"}]},
     {"sender":"bot","text":"**Found it.** There is a CronJob named `redis-cleanup` running every 30 minutes (`*/30 * * * *`), deployed 3 days ago [7]. It executes `redis-cli -h redis-sentinel.messaging.svc FLUSHDB` -- flushing the **production** Redis primary database every 30 minutes [8]. The label says `created-by: dev.intern`. Let me check GitHub for the commit that introduced this.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo3-0005","toolCalls":[{"id":"tc_demo3_010","tool_name":"github_rca","input":"{\"action\": \"commits\", \"repo\": \"acme-corp/platform\"}","output":"{\"status\": \"success\", \"commits\": [{\"sha\": \"f7a2c91\", \"message\": \"Merge PR #312: Add dev cache cleanup automation\", \"author\": \"dev.intern\", \"date\": \"2026-02-11T09:15:00Z\"}, {\"sha\": \"b3e8d12\", \"message\": \"Update notification-service replica count to 3\", \"author\": \"sarah.ops\", \"date\": \"2026-02-10T16:45:00Z\"}, {\"sha\": \"a1c5f67\", \"message\": \"Add Redis Sentinel monitoring dashboard\", \"author\": \"mike.sre\", \"date\": \"2026-02-09T11:20:00Z\"}], \"timeline_correlation\": \"Commit f7a2c91 (PR #312) deployed exactly 3 days before issue start. CRITICAL correlation.\"}","status":"completed","timestamp":"2026-02-14T14:32:45.000000"},{"id":"tc_demo3_011","tool_name":"github_rca","input":"{\"action\": \"diff\", \"repo\": \"acme-corp/platform\", \"sha\": \"f7a2c91\"}","output":"{\"status\": \"success\", \"action\": \"diff\", \"files_changed\": 1, \"additions\": 28, \"deletions\": 0, \"key_changes\": [{\"file\": \"k8s/messaging/cronjobs/redis-cleanup.yaml\", \"diff\": \"+ apiVersion: batch/v1\\n+ kind: CronJob\\n+ metadata:\\n+   name: redis-cleanup\\n+   namespace: messaging\\n+ spec:\\n+   schedule: \\\"*/30 * * * *\\\"\\n+   jobTemplate:\\n+     spec:\\n+       template:\\n+         spec:\\n+           containers:\\n+           - name: redis-cleanup\\n+             image: redis:7-alpine\\n+             command:\\n+             - /bin/sh\\n+             - -c\\n+             - redis-cli -h redis-sentinel.messaging.svc FLUSHDB\"}], \"pr_description\": \"Adds automated cleanup for dev Redis cache. Runs every 30 minutes to keep dev environment clean.\", \"review_comments\": \"No reviews were requested for this PR.\"}","status":"completed","timestamp":"2026-02-14T14:33:00.000000"}]},
     {"sender":"bot","text":"**Root cause confirmed.** PR #312 (\"Add dev cache cleanup automation\") was merged 3 days ago by dev.intern [9]. The PR description says it is for \"dev Redis cache cleanup\" but the manifest hardcodes `redis-sentinel.messaging.svc` -- which is the **production** Sentinel endpoint [10]. The PR had zero code reviews. Every 30 minutes, the CronJob flushes the production Redis primary, Sentinel detects the anomaly and triggers a failover. During the ~5 second failover, notification-service loses its Redis connection and returns 502s.\n\nLet me search for best practices on Redis failover resilience and then apply a fix.","message_number":7,"isCompleted":true,"run_id":"lc_run--demo3-0006","toolCalls":[{"id":"tc_demo3_012","tool_name":"web_search","input":"{\"query\": \"Redis Sentinel failover resilience ioredis retry strategy best practices\"}","output":"1. Redis Sentinel Failover Handling (redis.io): Client libraries should implement retry logic during Sentinel failovers. Failover typically takes 3-5 seconds.\n\n2. ioredis Sentinel Configuration (github.com/redis/ioredis): Use sentinelRetryStrategy and retryStrategy options. enableOfflineQueue buffers commands during disconnection.\n\n3. Kubernetes CronJob Best Practices (kubernetes.io): Always use namespace-scoped service references and validate target environments in CI/CD.","status":"completed","timestamp":"2026-02-14T14:33:30.000000"},{"id":"tc_demo3_013","tool_name":"cloud_exec","input":"{\"command\": \"kubectl delete cronjob redis-cleanup -n messaging\", \"provider\": \"aws\"}","output":"cronjob.batch \"redis-cleanup\" deleted","status":"completed","timestamp":"2026-02-14T14:33:45.000000"},{"id":"tc_demo3_014","tool_name":"github_fix","input":"{\"repo\": \"acme-corp/platform\", \"file_path\": \"services/notification-service/src/lib/redis.ts\", \"description\": \"Add Redis Sentinel retry logic with exponential backoff\"}","output":"Fix suggestion created: Replace direct Redis connection with Sentinel-aware ioredis configuration including retryStrategy, sentinelRetryStrategy, enableOfflineQueue, and error/reconnect event handlers.","status":"completed","timestamp":"2026-02-14T14:34:00.000000"},{"id":"tc_demo3_015","tool_name":"github_apply_fix","input":"{\"repo\": \"acme-corp/platform\", \"branch\": \"fix/aurora-redis-sentinel-retry\", \"title\": \"Fix: Add Redis Sentinel retry logic to notification-service\"}","output":"Pull request created successfully.\n- Branch: fix/aurora-redis-sentinel-retry\n- PR #315: https://github.com/acme-corp/platform/pull/315\n- Changes: services/notification-service/src/lib/redis.ts","status":"completed","timestamp":"2026-02-14T14:34:20.000000"}]},
     {"sender":"bot","text":"Investigation complete. Here is a summary of findings and actions taken:\n\n**Root Cause**: PR #312 (\"Add dev cache cleanup automation\", merged 3 days ago) deployed a CronJob that executes `FLUSHDB` against the production Redis Sentinel endpoint every 30 minutes. This was intended for the dev environment but targeted production due to a hardcoded service address. Each flush triggers a Sentinel failover, causing a ~5 second window where all Redis-dependent services lose connectivity, resulting in 502 errors.\n\n**Actions Taken**:\n1. Deleted the rogue `redis-cleanup` CronJob to immediately stop the recurring failovers [12]\n2. Created PR #315 with Redis Sentinel retry logic (ioredis sentinelRetryStrategy + exponential backoff) so the notification-service gracefully handles any future failovers\n\n**Recommended Follow-ups**:\n- Audit all CronJobs cluster-wide for hardcoded production endpoints\n- Add Kubernetes NetworkPolicies to prevent cross-environment access to production Redis\n- Require mandatory code review for all k8s manifest changes, especially CronJobs\n- Implement environment validation in the CI/CD pipeline","message_number":8,"isCompleted":true,"run_id":"lc_run--demo3-0007","toolCalls":[]}
   ]')::jsonb,
   '2026-02-14 14:30:00', '2026-02-14 14:45:00', true, 'completed', v_incident_id);

  RAISE NOTICE 'Demo incident 3 chat session inserted.';

  -- Fix tool call IDs, run_ids, and command display fields
  UPDATE chat_sessions SET messages = (
    SELECT jsonb_agg(
      CASE
        WHEN msg->'toolCalls' IS NOT NULL AND jsonb_typeof(msg->'toolCalls') = 'array' AND jsonb_array_length(msg->'toolCalls') > 0 THEN
          jsonb_set(msg, '{toolCalls}', (
            SELECT jsonb_agg(
              CASE
                WHEN updated_tc->>'tool_name' = 'cloud_exec' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'command')
                WHEN updated_tc->>'tool_name' = 'search_splunk' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'query')
                WHEN updated_tc->>'tool_name' = 'github_rca' THEN
                  updated_tc || jsonb_build_object('command', 'github: ' || COALESCE((updated_tc->>'input')::jsonb->>'action', 'rca'))
                WHEN updated_tc->>'tool_name' = 'web_search' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'query')
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

  RAISE NOTICE 'Demo incident 3 fully inserted.';

END $$;
