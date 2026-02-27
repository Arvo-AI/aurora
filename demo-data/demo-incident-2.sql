-- Demo Incident 2: Memory leak causing OOMKilled pods - checkout-service (AWS/EKS + Datadog)
-- Idempotent: checks if incident already exists before inserting.

DO $$
DECLARE
  v_incident_id UUID := 'b923d5a1-7e4c-4f8b-9a1d-2c5e8f3b6d90';
  v_chat_session_id VARCHAR(50) := 'c1a2b3d4-5e6f-7a8b-9c0d-1e2f3a4b5c6d';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_datadog_event_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  -- Check if already exists
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident 2 already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident 2...';

  -- 1. Insert datadog_events source record
  INSERT INTO datadog_events (user_id, event_type, event_title, status, scope, payload, received_at)
  VALUES (
    v_demo_user_id,
    'alert',
    'Memory leak causing OOMKilled pods - checkout-service',
    'alert',
    'checkout-service',
    '{
      "id": "evt_demo_002",
      "title": "Memory leak causing OOMKilled pods - checkout-service",
      "text": "Pod checkout-service-7f8d9b6c4-xk2p1 has been OOMKilled 5 times in the last hour. Memory usage reached 512Mi limit before each restart. Service availability degraded.",
      "date_happened": 1739462400,
      "priority": "normal",
      "alert_type": "error",
      "host": "ip-10-0-2-47.ec2.internal",
      "tags": ["env:production","service:checkout-service","kube_namespace:default","kube_cluster:eks-prod-us-east-1","team:platform","alert_type:oomkilled"],
      "source_type_name": "Kubernetes",
      "aggregation_key": "checkout-service-oom",
      "event_object": "checkout-service-7f8d9b6c4-xk2p1"
    }'::jsonb,
    '2026-02-13 10:00:00'
  ) RETURNING id INTO v_datadog_event_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id,
    v_demo_user_id,
    'datadog',
    v_datadog_event_id,
    'resolved',
    'critical',
    'Memory leak causing OOMKilled pods - checkout-service',
    'checkout-service',
    'production',
    'complete',
    -- aurora_summary (placeholder, replaced below)
    '',
    v_chat_session_id::uuid,
    '2026-02-13 10:00:00',
    '2026-02-13 10:12:00',
    'thoughts',
    '2026-02-13 10:00:00',
    '2026-02-13 10:12:00',
    jsonb_build_object(
      'is_demo', true,
      'datadog_event_id', 'evt_demo_002',
      'incidentId', 'evt_demo_002',
      'source_url', 'https://app.datadoghq.com/event/explorer?query=checkout-service'
    ),
    3,
    ARRAY['checkout-service', 'redis-cache', 'api-gateway'],
    -- visualization_code (placeholder, replaced below)
    '',
    '2026-02-13 10:12:00'
  );

  -- 3. Update summary
  UPDATE incidents SET aurora_summary = '# Incident Report: Memory Leak Causing OOMKilled Pods - checkout-service

## Summary

On February 13, 2026 at 10:00 UTC, the checkout-service running on the EKS production cluster (eks-prod-us-east-1) began experiencing repeated OOMKilled events. Pods were terminated after exceeding their 512Mi memory limit [1, 2], with 5 restarts observed within a single hour. Application logs confirmed JavaScript heap out of memory errors with the session cache growing to over 127,000 entries consuming approximately 248MB [3].

The root cause was a code change introduced in PR #47 ("Add session caching for faster checkout"), merged and deployed at 08:15 UTC [6, 7]. The change replaced Redis-based session lookups with an in-memory `Map()` used as a local cache in `services/checkout-service/src/handlers/checkout.js`. This Map had no eviction policy, TTL, or size limit [6]. Under production traffic of approximately 200 requests per second, each session entry consumed roughly 2KB, resulting in approximately 24MB per minute of allocations that were never freed. At this rate, the 512Mi container limit was hit every 15-20 minutes, matching the observed OOMKill restart interval. AWS CloudWatch metrics confirmed node memory utilization rose from 45% to 78% over the 2-hour period following the deployment [5].

The impact was moderate: checkout-service pods cycled through OOMKill restarts causing approximately 12% of checkout requests to fail with 503 errors. The api-gateway upstream propagated 5xx errors to end users. Three correlated Datadog alerts fired across the checkout-service and api-gateway monitors. The service was stabilized by temporarily increasing memory limits to 1Gi and restarting pods [10], while a permanent fix replacing the unbounded Map with an LRU cache (max 10,000 entries, 5-minute TTL) was submitted as PR #48.

## Suggested Next Steps

- **Review and merge PR #48**: The fix replaces the unbounded Map with a properly bounded LRU cache. Run load tests in staging to validate memory stays within limits under sustained traffic before deploying to production.
- **Add memory usage alerting**: Configure a Datadog monitor that alerts when pod memory usage exceeds 70% of the configured limit to provide early warning before OOMKill events occur.
- **Audit other in-memory caches**: Search the codebase for similar unbounded Map or Object usage patterns that could cause memory leaks in other services, particularly any changes introduced without corresponding eviction logic.
- **Enable heap profiling in staging**: Add the Node.js `--inspect` flag to staging deployments and periodically capture heap snapshots to detect memory growth patterns before they reach production.

## Code Fix Available

- **Fix checkout.js: Replace unbounded Map cache with LRU cache** to prevent the memory leak. The fix adds the lru-cache package with a maximum of 10,000 entries and 5-minute TTL, bounding memory to approximately 20MB under peak traffic.'
  WHERE id = v_incident_id;

  -- 4. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "checkout-svc", "label": "checkout-svc", "type": "service", "status": "degraded", "parentId": null},
    {"id": "redis-cache", "label": "redis-cache", "type": "database", "status": "healthy", "parentId": null},
    {"id": "api-gateway", "label": "api-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "rds-postgres", "label": "rds-postgres", "type": "database", "status": "healthy", "parentId": null},
    {"id": "session-cache", "label": "Session Cache (Map)", "type": "metric", "status": "failed", "parentId": "checkout-svc"},
    {"id": "memory-limit", "label": "Memory 512Mi", "type": "metric", "status": "failed", "parentId": "checkout-svc"},
    {"id": "alert-oomkill", "label": "OOMKilled Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-5xx", "label": "5xx Errors Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "pr-47", "label": "PR #47 Deploy", "type": "deployment", "status": "failed", "parentId": null},
    {"id": "pr-48-fix", "label": "PR #48 Fix", "type": "deployment", "status": "healthy", "parentId": null},
    {"id": "memory-leak", "label": "Unbounded Map", "type": "event", "status": "failed", "parentId": null}
  ],
  "edges": [
    {"source": "pr-47", "target": "checkout-svc", "label": "deployed code change", "type": "causation"},
    {"source": "pr-47", "target": "memory-leak", "label": "introduced unbounded cache", "type": "causation"},
    {"source": "memory-leak", "target": "session-cache", "label": "cache grows without limit", "type": "causation"},
    {"source": "session-cache", "target": "memory-limit", "label": "exceeded 512Mi", "type": "causation"},
    {"source": "memory-limit", "target": "alert-oomkill", "label": "triggered OOMKill", "type": "causation"},
    {"source": "checkout-svc", "target": "api-gateway", "label": "upstream 5xx errors", "type": "causation"},
    {"source": "api-gateway", "target": "alert-5xx", "label": "triggered alert", "type": "causation"},
    {"source": "checkout-svc", "target": "redis-cache", "label": "bypassed by local cache", "type": "dependency"},
    {"source": "checkout-svc", "target": "rds-postgres", "label": "reads order data", "type": "dependency"},
    {"source": "pr-48-fix", "target": "memory-leak", "label": "adds LRU eviction", "type": "causation"}
  ],
  "rootCauseId": "pr-47",
  "affectedIds": ["checkout-svc", "session-cache", "memory-limit", "alert-oomkill", "api-gateway", "alert-5xx", "memory-leak"],
  "version": 5,
  "updatedAt": "2026-02-13T10:12:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  -- 5. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'datadog', v_datadog_event_id, 'OOMKilled: checkout-service pod restarting', 'checkout-service', 'critical', 'primary', 1.0, '{}', '{"tags":["env:production","service:checkout-service"]}'::jsonb, '2026-02-13 10:00:00'),
  (v_demo_user_id, v_incident_id, 'datadog', v_datadog_event_id + 1, 'High memory usage on checkout-service nodes', 'checkout-service', 'warning', 'time_proximity', 0.92, '{"reason":"Memory spike correlated with OOMKill events"}'::jsonb, '{"tags":["env:production","service:checkout-service","monitor:memory"]}'::jsonb, '2026-02-13 10:01:00'),
  (v_demo_user_id, v_incident_id, 'datadog', v_datadog_event_id + 2, 'Increased 5xx errors on /api/checkout endpoint', 'api-gateway', 'warning', 'service_dependency', 0.87, '{"reason":"Downstream checkout-service pods restarting"}'::jsonb, '{"tags":["env:production","service:api-gateway","endpoint:/api/checkout"]}'::jsonb, '2026-02-13 10:02:00');

  -- Continue in next section (thoughts, citations, suggestions, chat)

  -- 6. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-13 10:02:01', 'Starting investigation of OOMKilled pods on checkout-service in the EKS production cluster.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:05', 'Searching knowledge base for EKS memory troubleshooting and OOMKill runbooks.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:10', 'Found a relevant runbook for Kubernetes OOMKill investigation on EKS clusters.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:15', 'The runbook recommends checking pod memory limits, recent deployments, and heap profiles. Let me start with kubectl.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:25', 'Running kubectl get pods to check the current state of checkout-service pods.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:35', 'Multiple checkout-service pods show OOMKilled status with 5+ restarts. The pods are in a CrashLoopBackOff cycle.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:45', 'Let me describe the pod to see the memory limits and last termination reason.', 'analysis'),
  (v_incident_id, '2026-02-13 10:02:55', 'Pod has a 512Mi memory limit. Last termination reason is OOMKilled with exit code 137. The container was using 511Mi right before termination.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:05', 'Checking pod logs for any memory-related errors or warnings.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:15', 'Logs show "FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory" errors repeating every 10-15 minutes. This confirms a memory leak.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:25', 'Running kubectl top pods to see current memory consumption across all checkout-service replicas.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:35', 'Memory is growing linearly across all 3 replicas. Pod 1: 487Mi, Pod 2: 423Mi, Pod 3: 391Mi. All trending toward the 512Mi limit.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:45', 'This is a clear memory leak pattern. Let me check AWS CloudWatch for node-level metrics to understand the broader impact.', 'analysis'),
  (v_incident_id, '2026-02-13 10:03:55', 'AWS CloudWatch shows node memory utilization spiked from 45% to 78% over the last 2 hours, correlating with pod restarts.', 'analysis'),
  (v_incident_id, '2026-02-13 10:04:05', 'Now let me correlate with recent deployments. Checking GitHub for recent commits and deployments.', 'analysis'),
  (v_incident_id, '2026-02-13 10:04:15', 'Correlated alert detected: "High memory usage on checkout-service nodes" - this aligns with the OOMKill pattern.', 'analysis'),
  (v_incident_id, '2026-02-13 10:04:25', 'Another correlated alert: "Increased 5xx errors on /api/checkout endpoint" - downstream impact from pod restarts.', 'analysis'),
  (v_incident_id, '2026-02-13 10:04:35', 'Checking GitHub deployment history for the checkout-service.', 'analysis'),
  (v_incident_id, '2026-02-13 10:04:45', 'Found a deployment at 08:15 UTC today - PR #47: "Add session caching for faster checkout" by developer alex.chen. This was deployed roughly 2 hours before the first OOMKill.', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:00', 'This is highly suspicious. A caching change deployed right before memory issues started. Let me examine the PR diff.', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:15', 'Analyzing the diff from PR #47. The change modifies services/checkout-service/src/handlers/checkout.js.', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:30', 'Found the issue. PR #47 introduced a local in-memory Map as a session cache to avoid Redis lookups:', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:35', 'The code creates a module-level `const sessionCache = new Map()` and adds entries on every checkout request, but never removes them. There is no TTL, no max size, and no eviction policy.', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:45', 'Under production traffic of ~200 requests/second, each session object is roughly 2KB. That is 400KB/second or ~24MB/minute of new allocations that are never freed.', 'analysis'),
  (v_incident_id, '2026-02-13 10:05:55', 'At that rate, the 512Mi limit would be hit in approximately 20 minutes after the initial heap baseline. This matches the ~15 minute restart interval we see in the logs.', 'analysis'),
  (v_incident_id, '2026-02-13 10:06:05', 'Let me search the web for best practices on Node.js in-memory caching to confirm the fix approach.', 'analysis'),
  (v_incident_id, '2026-02-13 10:06:15', 'Web search confirms: unbounded Map/Object caches are one of the most common Node.js memory leak patterns. The recommended fix is to use an LRU cache with a max size and TTL.', 'analysis'),
  (v_incident_id, '2026-02-13 10:06:25', 'The popular lru-cache npm package is the standard solution. It supports maxSize, TTL, and automatic eviction.', 'analysis'),
  (v_incident_id, '2026-02-13 10:06:35', 'Root cause confirmed: PR #47 introduced an unbounded in-memory session cache (Map) in checkout.js that grows without limit under production traffic, causing OOMKill.', 'analysis'),
  (v_incident_id, '2026-02-13 10:06:45', 'Generating a code fix suggestion: replace the Map with an LRU cache.', 'analysis'),
  (v_incident_id, '2026-02-13 10:07:00', 'The fix replaces `new Map()` with `new LRUCache({ max: 10000, ttl: 300000 })` (10K entries max, 5-minute TTL). This bounds memory to approximately 20MB worst case.', 'analysis'),
  (v_incident_id, '2026-02-13 10:07:15', 'Also recommending immediate mitigation: restart pods with temporarily increased memory limit (1Gi) to stabilize while the fix is deployed.', 'analysis'),
  (v_incident_id, '2026-02-13 10:07:30', 'Creating PR #48 with the fix on branch fix/aurora-checkout-oom-leak.', 'analysis'),
  (v_incident_id, '2026-02-13 10:07:45', 'PR #48 created successfully. The fix includes adding lru-cache to package.json and updating the checkout handler.', 'analysis'),
  (v_incident_id, '2026-02-13 10:08:00', 'Applying immediate mitigation: restarting pods with increased memory limit.', 'analysis'),
  (v_incident_id, '2026-02-13 10:08:15', 'Pods restarted successfully with 1Gi memory limit. All 3 replicas are now Running and healthy.', 'analysis'),
  (v_incident_id, '2026-02-13 10:08:30', 'Recommending additional prevention measure: add a Datadog monitor for memory usage at 70% of limit to catch leaks before they cause OOMKills.', 'analysis'),
  (v_incident_id, '2026-02-13 10:08:45', 'Also recommending adding Node.js heap snapshot tooling (--inspect flag) to the staging environment for proactive memory profiling.', 'analysis'),
  (v_incident_id, '2026-02-13 10:09:00', 'Investigation complete. Root cause identified, fix created as PR #48, and immediate mitigation applied.', 'analysis');

  -- 7. Insert citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Cloud CLI', 'kubectl get pods -l app=checkout-service -o wide', 'NAME                               READY   STATUS             RESTARTS      AGE     IP            NODE
checkout-svc-7f8d9b6c4-xk2p1      0/1     OOMKilled          5 (2m ago)    2h15m   10.0.2.47     ip-10-0-2-47.ec2.internal
checkout-svc-7f8d9b6c4-mn3q2      1/1     Running            4 (8m ago)    2h15m   10.0.3.22     ip-10-0-3-22.ec2.internal
checkout-svc-7f8d9b6c4-yz9r8      1/1     Running            3 (15m ago)   2h15m   10.0.1.89     ip-10-0-1-89.ec2.internal', '2026-02-13 10:02:30'),
  (v_incident_id, '2', 'Cloud CLI', 'kubectl describe pod checkout-svc-7f8d9b6c4-xk2p1', 'Name:         checkout-svc-7f8d9b6c4-xk2p1
Namespace:    default
Node:         ip-10-0-2-47.ec2.internal/10.0.2.47
Status:       Running
Containers:
  checkout:
    Image:      123456789.dkr.ecr.us-east-1.amazonaws.com/checkout-service:v2.3.47
    Limits:
      memory:  512Mi
    Requests:
      memory:  256Mi
    State:       Running (Started: 2026-02-13 10:00:12 +0000 UTC)
    Last State:  Terminated (Reason: OOMKilled, Exit Code: 137, Started: 2026-02-13 09:45:01, Finished: 2026-02-13 09:59:58)
Events:
  Warning  OOMKilling  2m (x5 over 1h55m)  kubelet  Memory usage exceeded 512Mi limit', '2026-02-13 10:02:50'),
  (v_incident_id, '3', 'Cloud CLI', 'kubectl logs checkout-svc-7f8d9b6c4-xk2p1 --tail=30', 'FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory
 1: 0xb090e0 node::Abort() [node]
 2: 0xa1b70e v8::Utils::ReportOOMFailure() [node]
{"level":"error","message":"Uncaught exception: heap out of memory","timestamp":"2026-02-13T09:59:55.123Z"}
{"level":"warn","message":"Session cache size: 127453 entries, estimated memory: 248MB","timestamp":"2026-02-13T09:59:50.001Z"}
{"level":"info","message":"Processing checkout request for session sess_8f2a1b","timestamp":"2026-02-13T09:59:49.892Z"}
{"level":"info","message":"Cache hit for session sess_7e3c9d","timestamp":"2026-02-13T09:59:49.850Z"}
{"level":"warn","message":"Session cache size: 127401 entries, estimated memory: 247MB","timestamp":"2026-02-13T09:58:50.002Z"}', '2026-02-13 10:03:10'),
  (v_incident_id, '4', 'Cloud CLI', 'kubectl top pods -l app=checkout-service', 'NAME                               CPU(cores)   MEMORY(bytes)
checkout-svc-7f8d9b6c4-xk2p1      245m         487Mi
checkout-svc-7f8d9b6c4-mn3q2      198m         423Mi
checkout-svc-7f8d9b6c4-yz9r8      167m         391Mi', '2026-02-13 10:03:30'),
  (v_incident_id, '5', 'Cloud CLI', 'aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name MemoryUtilization --dimensions Name=AutoScalingGroupName,Value=eks-prod-checkout-ng --start-time 2026-02-13T06:00:00Z --end-time 2026-02-13T10:05:00Z --period 600 --statistics Average', '{"Datapoints": [
  {"Timestamp": "2026-02-13T06:00:00Z", "Average": 42.3},
  {"Timestamp": "2026-02-13T07:00:00Z", "Average": 44.1},
  {"Timestamp": "2026-02-13T08:00:00Z", "Average": 45.7},
  {"Timestamp": "2026-02-13T08:30:00Z", "Average": 52.4},
  {"Timestamp": "2026-02-13T09:00:00Z", "Average": 61.8},
  {"Timestamp": "2026-02-13T09:30:00Z", "Average": 70.2},
  {"Timestamp": "2026-02-13T10:00:00Z", "Average": 78.1}
]}', '2026-02-13 10:04:00'),
  (v_incident_id, '6', 'MCP: GitHub', 'github_rca: recent commits + PR #47 diff', '{
  "recent_deployments": [
    {"sha": "a3f8c21", "message": "Merge PR #47: Add session caching for faster checkout", "author": "alex.chen", "date": "2026-02-13T08:15:00Z", "url": "https://github.com/acme-corp/platform/pull/47"}
  ],
  "diff_summary": {
    "files_changed": 2,
    "additions": 24,
    "deletions": 8,
    "key_changes": [
      "services/checkout-service/src/handlers/checkout.js: Added module-level Map() as session cache, storing session data on every request without eviction",
      "services/checkout-service/package.json: No new dependencies added (used built-in Map)"
    ]
  }
}', '2026-02-13 10:05:20'),
  (v_incident_id, '7', 'MCP: GitHub', 'github_rca: list recent commits on checkout-service', '{
  "commits": [
    {"sha": "a3f8c21", "message": "Merge PR #47: Add session caching for faster checkout", "author": "alex.chen", "date": "2026-02-13T08:15:00Z"},
    {"sha": "e7b2d45", "message": "Update checkout API docs", "author": "sarah.kim", "date": "2026-02-12T16:30:00Z"},
    {"sha": "c9a1f33", "message": "Fix currency conversion rounding", "author": "alex.chen", "date": "2026-02-11T14:20:00Z"},
    {"sha": "b5d4e78", "message": "Add retry logic for payment gateway timeout", "author": "mike.jones", "date": "2026-02-10T11:45:00Z"}
  ],
  "timeline_correlation": "Commit a3f8c21 (PR #47) deployed 1h45m before first OOMKill event. HIGH correlation."
}', '2026-02-13 10:04:40'),
  (v_incident_id, '8', 'Web Search', 'Node.js Map memory leak unbounded cache best practices', 'Search results summary:

1. **Node.js Memory Leak Patterns** (nodejs.org/guides): "Using Map or Object as an unbounded cache is one of the most common memory leak patterns in Node.js applications. Every entry added without a corresponding delete will persist for the lifetime of the process."

2. **lru-cache npm package** (npmjs.com): "A cache object that deletes the least-recently-used items. Supports max entries, max size in bytes, TTL, and dispose callbacks. 45M weekly downloads."

3. **Kubernetes OOMKilled Troubleshooting** (kubernetes.io): "Exit code 137 indicates the container was killed by SIGKILL, typically due to exceeding memory limits. Check resource.limits.memory and application memory usage patterns."

Recommendation: Replace unbounded Map with lru-cache package configured with max entries and TTL.', '2026-02-13 10:06:10'),
  (v_incident_id, '9', 'Knowledge Base', 'Search: EKS memory troubleshooting OOMKill runbook', 'Found: "EKS Pod Memory Troubleshooting Runbook"

Key steps:
1. Check pod memory limits: kubectl describe pod <name>
2. Check actual usage: kubectl top pods
3. Check node-level metrics: aws cloudwatch get-metric-statistics
4. Check for memory leaks: review recent deployments and code changes
5. Immediate mitigation: increase memory limits or scale horizontally
6. Long-term fix: identify and fix the leak, add memory monitoring alerts

Tags: kubernetes, eks, aws, memory, oomkill, troubleshooting', '2026-02-13 10:02:08'),
  (v_incident_id, '10', 'Cloud CLI', 'kubectl set resources deployment/checkout-svc --limits=memory=1Gi && kubectl rollout restart deployment/checkout-svc', 'deployment.apps/checkout-svc resource requirements updated
deployment.apps/checkout-svc restarted
Waiting for rollout to finish: 1 of 3 updated replicas are available...
Waiting for rollout to finish: 2 of 3 updated replicas are available...
deployment "checkout-svc" successfully rolled out', '2026-02-13 10:08:10');

  -- 8. Insert suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Check pod memory limits and requests', 'Review the current memory limits and requests for checkout-service pods to verify they match expected workload requirements. The current 512Mi limit may be too low even after fixing the leak.', 'diagnostic', 'safe', 'kubectl describe deployment checkout-svc | grep -A5 "Limits\|Requests"', '2026-02-13 10:07:00'),
  (v_incident_id, 'Review Node.js heap snapshots', 'Enable Node.js heap profiling in the staging environment to proactively detect memory growth patterns before they reach production. Use --inspect flag and Chrome DevTools.', 'diagnostic', 'safe', 'kubectl exec -it checkout-svc-7f8d9b6c4-mn3q2 -- node --inspect=0.0.0.0:9229 src/index.js', '2026-02-13 10:07:10'),
  (v_incident_id, 'Restart affected pods with increased memory limit', 'Immediately increase memory limit to 1Gi and restart all checkout-service pods to stabilize the service while the code fix is deployed.', 'mitigation', 'low', 'kubectl set resources deployment/checkout-svc --limits=memory=1Gi && kubectl rollout restart deployment/checkout-svc', '2026-02-13 10:07:20');

  -- Insert the fix suggestion with code diff and PR
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Fix checkout.js: Replace unbounded Map cache with LRU cache to prevent memory leak',
   'The session cache introduced in PR #47 uses an unbounded Map that grows without limit. Replace it with lru-cache (max 10,000 entries, 5-minute TTL) to bound memory usage to approximately 20MB.',
   'fix', 'medium', NULL,
   'services/checkout-service/src/handlers/checkout.js',
   E'const express = require(''express'');\nconst redis = require(''./redis-client'');\n\n// Session cache for faster checkout\nconst sessionCache = new Map();\n\nasync function handleCheckout(req, res) {\n  const sessionId = req.headers[''x-session-id''];\n  \n  // Check local cache first\n  let session = sessionCache.get(sessionId);\n  if (!session) {\n    session = await redis.get(`session:${sessionId}`);\n    sessionCache.set(sessionId, session);\n  }\n  \n  // Process checkout\n  const result = await processOrder(session, req.body);\n  res.json(result);\n}',
   E'const express = require(''express'');\nconst redis = require(''./redis-client'');\nconst { LRUCache } = require(''lru-cache'');\n\n// Session cache with bounded size and TTL\nconst sessionCache = new LRUCache({\n  max: 10000,\n  ttl: 1000 * 60 * 5, // 5 minutes\n});\n\nasync function handleCheckout(req, res) {\n  const sessionId = req.headers[''x-session-id''];\n  \n  // Check local cache first\n  let session = sessionCache.get(sessionId);\n  if (!session) {\n    session = await redis.get(`session:${sessionId}`);\n    sessionCache.set(sessionId, session);\n  }\n  \n  // Process checkout\n  const result = await processOrder(session, req.body);\n  res.json(result);\n}',
   'acme-corp/platform',
   'https://github.com/acme-corp/platform/pull/48',
   48,
   'fix/aurora-checkout-oom-leak',
   '2026-02-13 10:07:30');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Add memory usage alerting at 70% threshold', 'Create a Datadog monitor that alerts when pod memory usage exceeds 70% of the configured limit. This provides early warning before OOMKill events occur.', 'mitigation', 'safe', NULL, '2026-02-13 10:08:30');

  -- 9. Insert chat session with tool calls matching the real format
  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: Memory leak causing OOMKilled pods - checkout-service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - DATADOG ALERT\n\n## ALERT DETAILS:\n- **Title**: Memory leak causing OOMKilled pods - checkout-service\n- **Status**: alert (priority: normal)\n- **Source**: datadog\n- **Labels/Tags**: env:production, service:checkout-service, kube_namespace:default, kube_cluster:eks-prod-us-east-1, team:platform, alert_type:oomkilled\n- **Host**: ip-10-0-2-47.ec2.internal\n\n## CONNECTED PROVIDERS:\nYou have access to: aws, datadog, github, github_repo_selection\n\n## GITHUB REPOSITORY CONTEXT:\n- **Connected Repository**: acme-corp/platform\n- **Branch**: main\n\n### GITHUB RCA TOOL (RECOMMENDED):\nUse the unified `github_rca` tool for efficient GitHub investigation.\n\n**IMPORTANT: Repository Resolution**\n- Omit `repo=` to auto-resolve from Knowledge Base.\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis following the ReAct methodology.\nUse all available tools to investigate: check pod status, logs, metrics, recent deployments, and code changes.\nCorrelate the timeline of the alert with recent changes.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo2-0001-aaaa-bbbb-000000000001","toolCalls":[{"id":"tc_demo2_001","tool_name":"knowledge_base_search","input":"{\"query\": \"kubernetes OOMKilled memory leak EKS pod troubleshooting\", \"limit\": 5}","output":"Found 3 relevant result(s) for: ''kubernetes OOMKilled memory leak EKS pod troubleshooting''\n\n1. **EKS Pod Memory Troubleshooting Runbook** (score: 0.91)\n   Key steps: Check pod memory limits, check actual usage with kubectl top, check node-level CloudWatch metrics, review recent deployments, identify and fix the leak.\n\n2. **Kubernetes OOMKill Investigation Guide** (score: 0.85)\n   When a pod is OOMKilled (exit code 137), check: container memory limits vs actual usage, application heap settings, recent code changes that may increase memory.\n\n3. **Node.js Memory Best Practices** (score: 0.78)\n   Common causes: unbounded caches, event listener leaks, large buffer allocations. Use --max-old-space-size flag and heap snapshots for diagnosis.","status":"completed","timestamp":"2026-02-13T10:02:08.000000","tool_name":"knowledge_base_search"},{"id":"tc_demo2_002","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get pods -l app=checkout-service -o wide\", \"provider\": \"aws\"}","output":"NAME                               READY   STATUS             RESTARTS      AGE     IP            NODE\ncheckout-svc-7f8d9b6c4-xk2p1      0/1     OOMKilled          5 (2m ago)    2h15m   10.0.2.47     ip-10-0-2-47.ec2.internal\ncheckout-svc-7f8d9b6c4-mn3q2      1/1     Running            4 (8m ago)    2h15m   10.0.3.22     ip-10-0-3-22.ec2.internal\ncheckout-svc-7f8d9b6c4-yz9r8      1/1     Running            3 (15m ago)   2h15m   10.0.1.89     ip-10-0-1-89.ec2.internal","status":"completed","timestamp":"2026-02-13T10:02:30.000000","tool_name":"cloud_exec"},{"id":"tc_demo2_003","tool_name":"cloud_exec","input":"{\"command\": \"kubectl describe pod checkout-svc-7f8d9b6c4-xk2p1\", \"provider\": \"aws\"}","output":"Name:         checkout-svc-7f8d9b6c4-xk2p1\nNamespace:    default\nNode:         ip-10-0-2-47.ec2.internal/10.0.2.47\nStatus:       Running\nContainers:\n  checkout:\n    Image:      123456789.dkr.ecr.us-east-1.amazonaws.com/checkout-service:v2.3.47\n    Limits:\n      memory:  512Mi\n    Requests:\n      memory:  256Mi\n    State:       Running (Started: 2026-02-13 10:00:12 +0000 UTC)\n    Last State:  Terminated (Reason: OOMKilled, Exit Code: 137, Started: 2026-02-13 09:45:01, Finished: 2026-02-13 09:59:58)\nEvents:\n  Warning  OOMKilling  2m (x5 over 1h55m)  kubelet  Memory usage exceeded 512Mi limit","status":"completed","timestamp":"2026-02-13T10:02:50.000000","tool_name":"cloud_exec"}]},
     {"sender":"bot","text":"Found a relevant EKS memory troubleshooting runbook [9]. Multiple checkout-service pods show OOMKilled status with 3-5 restarts [1]. Pod has a 512Mi memory limit with last termination reason OOMKilled, exit code 137 [2]. Let me check the application logs and current memory consumption.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo2-0002-aaaa-bbbb-000000000002","toolCalls":[{"id":"tc_demo2_004","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs checkout-svc-7f8d9b6c4-xk2p1 --tail=30\", \"provider\": \"aws\"}","output":"FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory\n 1: 0xb090e0 node::Abort() [node]\n 2: 0xa1b70e v8::Utils::ReportOOMFailure() [node]\n{\"level\":\"error\",\"message\":\"Uncaught exception: heap out of memory\",\"timestamp\":\"2026-02-13T09:59:55.123Z\"}\n{\"level\":\"warn\",\"message\":\"Session cache size: 127453 entries, estimated memory: 248MB\",\"timestamp\":\"2026-02-13T09:59:50.001Z\"}\n{\"level\":\"info\",\"message\":\"Processing checkout request for session sess_8f2a1b\",\"timestamp\":\"2026-02-13T09:59:49.892Z\"}\n{\"level\":\"info\",\"message\":\"Cache hit for session sess_7e3c9d\",\"timestamp\":\"2026-02-13T09:59:49.850Z\"}\n{\"level\":\"warn\",\"message\":\"Session cache size: 127401 entries, estimated memory: 247MB\",\"timestamp\":\"2026-02-13T09:58:50.002Z\"}","status":"completed","timestamp":"2026-02-13T10:03:10.000000","tool_name":"cloud_exec"},{"id":"tc_demo2_005","tool_name":"cloud_exec","input":"{\"command\": \"kubectl top pods -l app=checkout-service\", \"provider\": \"aws\"}","output":"NAME                               CPU(cores)   MEMORY(bytes)\ncheckout-svc-7f8d9b6c4-xk2p1      245m         487Mi\ncheckout-svc-7f8d9b6c4-mn3q2      198m         423Mi\ncheckout-svc-7f8d9b6c4-yz9r8      167m         391Mi","status":"completed","timestamp":"2026-02-13T10:03:30.000000","tool_name":"cloud_exec"}]},
     {"sender":"bot","text":"Logs confirm JavaScript heap out of memory errors [3]. The logs explicitly show the session cache growing to 127,453 entries consuming approximately 248MB. All 3 replicas are trending toward the 512Mi limit: 487Mi, 423Mi, 391Mi [4]. This is a clear memory leak pattern. Let me check AWS CloudWatch for node-level metrics and correlate with recent deployments.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo2-0003-aaaa-bbbb-000000000003","toolCalls":[{"id":"tc_demo2_006","tool_name":"cloud_exec","input":"{\"command\": \"aws cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name MemoryUtilization --dimensions Name=AutoScalingGroupName,Value=eks-prod-checkout-ng --start-time 2026-02-13T06:00:00Z --end-time 2026-02-13T10:05:00Z --period 600 --statistics Average\", \"provider\": \"aws\"}","output":"{\"Datapoints\": [{\"Timestamp\": \"2026-02-13T06:00:00Z\", \"Average\": 42.3}, {\"Timestamp\": \"2026-02-13T07:00:00Z\", \"Average\": 44.1}, {\"Timestamp\": \"2026-02-13T08:00:00Z\", \"Average\": 45.7}, {\"Timestamp\": \"2026-02-13T08:30:00Z\", \"Average\": 52.4}, {\"Timestamp\": \"2026-02-13T09:00:00Z\", \"Average\": 61.8}, {\"Timestamp\": \"2026-02-13T09:30:00Z\", \"Average\": 70.2}, {\"Timestamp\": \"2026-02-13T10:00:00Z\", \"Average\": 78.1}]}","status":"completed","timestamp":"2026-02-13T10:04:00.000000","tool_name":"cloud_exec"},{"id":"tc_demo2_007","tool_name":"github_rca","input":"{\"action\": \"commits\", \"repo\": \"acme-corp/platform\"}","output":"{\"status\": \"success\", \"action\": \"commits\", \"repository\": \"acme-corp/platform\", \"commits\": [{\"sha\": \"a3f8c21\", \"message\": \"Merge PR #47: Add session caching for faster checkout\", \"author\": \"alex.chen\", \"date\": \"2026-02-13T08:15:00Z\"}, {\"sha\": \"e7b2d45\", \"message\": \"Update checkout API docs\", \"author\": \"sarah.kim\", \"date\": \"2026-02-12T16:30:00Z\"}, {\"sha\": \"c9a1f33\", \"message\": \"Fix currency conversion rounding\", \"author\": \"alex.chen\", \"date\": \"2026-02-11T14:20:00Z\"}, {\"sha\": \"b5d4e78\", \"message\": \"Add retry logic for payment gateway timeout\", \"author\": \"mike.jones\", \"date\": \"2026-02-10T11:45:00Z\"}], \"timeline_correlation\": \"Commit a3f8c21 (PR #47) deployed 1h45m before first OOMKill event. HIGH correlation.\"}","status":"completed","timestamp":"2026-02-13T10:04:40.000000","tool_name":"github_rca"}]},
     {"sender":"bot","text":"AWS CloudWatch shows node memory jumped from 45% to 78% over 2 hours, starting right around 08:15 UTC [5]. GitHub shows a deployment at exactly that time - PR #47: \"Add session caching for faster checkout\" by alex.chen [7]. This is highly suspicious. A caching change deployed right before memory issues started. Let me examine the PR diff.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo2-0004-aaaa-bbbb-000000000004","toolCalls":[{"id":"tc_demo2_008","tool_name":"github_rca","input":"{\"action\": \"diff\", \"repo\": \"acme-corp/platform\", \"sha\": \"a3f8c21\"}","output":"{\"status\": \"success\", \"action\": \"diff\", \"files_changed\": 2, \"additions\": 24, \"deletions\": 8, \"key_changes\": [{\"file\": \"services/checkout-service/src/handlers/checkout.js\", \"diff\": \"+ const sessionCache = new Map();\\n+ \\n  async function handleCheckout(req, res) {\\n    const sessionId = req.headers[''x-session-id''];\\n-   const session = await redis.get(`session:${sessionId}`);\\n+   let session = sessionCache.get(sessionId);\\n+   if (!session) {\\n+     session = await redis.get(`session:${sessionId}`);\\n+     sessionCache.set(sessionId, session);\\n+   }\"}]}","status":"completed","timestamp":"2026-02-13T10:05:20.000000","tool_name":"github_rca"},{"id":"tc_demo2_009","tool_name":"web_search","input":"{\"query\": \"Node.js Map memory leak unbounded cache best practices LRU\"}","output":"Search results summary:\n\n1. **Node.js Memory Leak Patterns** (nodejs.org/guides): Using Map or Object as an unbounded cache is one of the most common memory leak patterns in Node.js applications. Every entry added without a corresponding delete will persist for the lifetime of the process.\n\n2. **lru-cache npm package** (npmjs.com): A cache object that deletes the least-recently-used items. Supports max entries, max size in bytes, TTL, and dispose callbacks. 45M weekly downloads.\n\n3. **Kubernetes OOMKilled Troubleshooting** (kubernetes.io): Exit code 137 indicates the container was killed by SIGKILL, typically due to exceeding memory limits.","status":"completed","timestamp":"2026-02-13T10:06:10.000000","tool_name":"web_search"}]},
     {"sender":"bot","text":"**Root cause identified.** PR #47 introduced a module-level `const sessionCache = new Map()` in `checkout.js` [6]. Every checkout request adds a session entry but entries are never removed - no TTL, no max size, no eviction policy. Under production traffic of ~200 req/s with ~2KB per session, that is ~24MB/minute of allocations that are never freed. The 512Mi limit is hit every ~15 minutes, matching the observed OOMKill restart interval.\n\nWeb search confirms this is a well-documented Node.js anti-pattern [8]. The recommended fix is to use the `lru-cache` package with max entries and TTL.\n\nLet me generate a fix and apply immediate mitigation.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo2-0005-aaaa-bbbb-000000000005","toolCalls":[{"id":"tc_demo2_010","tool_name":"github_fix","input":"{\"repo\": \"acme-corp/platform\", \"file_path\": \"services/checkout-service/src/handlers/checkout.js\", \"description\": \"Replace unbounded Map cache with LRU cache to prevent memory leak\"}","output":"Fix suggestion created. Original code uses `new Map()` with no eviction. Suggested fix: replace with `new LRUCache({ max: 10000, ttl: 300000 })` from lru-cache package. Memory bounded to approximately 20MB under peak traffic.","status":"completed","timestamp":"2026-02-13T10:07:00.000000","tool_name":"github_fix"},{"id":"tc_demo2_011","tool_name":"github_apply_fix","input":"{\"repo\": \"acme-corp/platform\", \"branch\": \"fix/aurora-checkout-oom-leak\", \"title\": \"Fix: Replace unbounded Map cache with LRU cache in checkout handler\"}","output":"Pull request created successfully.\n- Branch: fix/aurora-checkout-oom-leak\n- PR #48: https://github.com/acme-corp/platform/pull/48\n- Changes: services/checkout-service/src/handlers/checkout.js, package.json","status":"completed","timestamp":"2026-02-13T10:07:30.000000","tool_name":"github_apply_fix"},{"id":"tc_demo2_012","tool_name":"cloud_exec","input":"{\"command\": \"kubectl set resources deployment/checkout-svc --limits=memory=1Gi && kubectl rollout restart deployment/checkout-svc\", \"provider\": \"aws\"}","output":"deployment.apps/checkout-svc resource requirements updated\ndeployment.apps/checkout-svc restarted\nWaiting for rollout to finish: 1 of 3 updated replicas are available...\nWaiting for rollout to finish: 2 of 3 updated replicas are available...\ndeployment \"checkout-svc\" successfully rolled out","status":"completed","timestamp":"2026-02-13T10:08:10.000000","tool_name":"cloud_exec"}]},
     {"sender":"bot","text":"Investigation complete. Here is a summary of findings and actions taken:\n\n**Root Cause**: PR #47 (\"Add session caching for faster checkout\", merged 08:15 UTC) introduced an unbounded `Map()` as a session cache in `checkout.js`. The cache grows without limit under production traffic, causing pods to exceed their 512Mi memory limit and get OOMKilled every ~15 minutes.\n\n**Actions Taken**:\n1. Created PR #48 with fix: replaces unbounded Map with `lru-cache` (max 10,000 entries, 5-minute TTL), bounding memory to ~20MB\n2. Applied immediate mitigation: increased memory limit to 1Gi and restarted all pods [10] - all 3 replicas now running and healthy\n\n**Recommended Follow-ups**:\n- Review and merge PR #48 after staging load tests\n- Add Datadog memory alert at 70% threshold for early warning\n- Audit other services for similar unbounded cache patterns","message_number":7,"isCompleted":true,"run_id":"lc_run--demo2-0006-aaaa-bbbb-000000000006","toolCalls":[]}
   ]')::jsonb,
   '2026-02-13 10:02:00', '2026-02-13 10:12:00', true, 'completed', v_incident_id);

  RAISE NOTICE 'Demo incident 2 inserted successfully.';

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

END $$;

-- Ensure resolved status on existing installs
UPDATE incidents SET status = 'resolved' WHERE id = 'b923d5a1-7e4c-4f8b-9a1d-2c5e8f3b6d90' AND status != 'resolved';

-- Postmortem for incident 2 (idempotent)
INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'b923d5a1-7e4c-4f8b-9a1d-2c5e8f3b6d90',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_2$# Postmortem: Memory leak causing OOMKilled pods - checkout-service

**Date:** 2026-02-13 10:00 UTC
**Duration:** 12m
**Severity:** critical
**Service:** checkout-service
**Source:** datadog

## Summary
The checkout-service experienced repeated OOMKilled events due to an unbounded in-memory session cache introduced in PR #47. Pods exceeded their 512Mi memory limit approximately every 15-20 minutes under production traffic, causing service disruptions with 12% of checkout requests failing with 503 errors. The service was stabilized by increasing memory limits to 1Gi and a permanent fix was implemented using an LRU cache with bounded size and TTL.

## Timeline
- **10:00:00** - Incident detected: checkout-service pods experiencing OOMKilled events
- **10:02:01** - Investigation started on OOMKilled pods in EKS production cluster
- **10:02:35** - Confirmed multiple checkout-service pods in CrashLoopBackOff with 5+ restarts
- **10:02:55** - Pod description revealed 512Mi memory limit with OOMKilled termination reason (exit code 137)
- **10:03:15** - Pod logs showed JavaScript heap out of memory errors repeating every 10-15 minutes
- **10:03:35** - Memory consumption confirmed growing linearly across all 3 replicas (487Mi, 423Mi, 391Mi)
- **10:03:55** - AWS CloudWatch showed node memory utilization spike from 45% to 78% over 2 hours
- **10:04:45** - Identified deployment at 08:15 UTC: PR #47 "Add session caching for faster checkout"
- **10:05:35** - Root cause identified: unbounded Map cache with no eviction policy in checkout.js
- **10:05:55** - Calculated memory growth rate: ~24MB/minute under 200 req/sec traffic
- **10:07:30** - Created PR #48 with LRU cache fix (max 10K entries, 5-minute TTL)
- **10:08:15** - Applied mitigation: restarted pods with 1Gi memory limit
- **10:08:30** - All 3 replicas running healthy with increased memory limit
- **10:09:00** - Investigation complete, service stabilized

## Root Cause
PR #47, deployed at 08:15 UTC on 2026-02-13, introduced an in-memory session cache using a JavaScript Map in `services/checkout-service/src/handlers/checkout.js`. This cache had no eviction policy, TTL, or size limit. Under production traffic of approximately 200 requests per second, with each session entry consuming roughly 2KB, the cache grew at a rate of ~24MB per minute. This unbounded growth caused pods to exceed their 512Mi memory limit every 15-20 minutes, triggering OOMKilled events and pod restarts.

## Impact
- **Service Availability**: checkout-service pods cycled through OOMKill restarts, causing approximately 12% of checkout requests to fail with 503 errors
- **User Experience**: End users received 5xx errors from the api-gateway when checkout-service pods were restarting
- **Infrastructure**: Node memory utilization increased from 45% to 78% on the EKS production cluster (eks-prod-us-east-1)
- **Monitoring**: Three correlated Datadog alerts fired across checkout-service and api-gateway monitors
- **Duration**: Service degradation occurred for approximately 2 hours from initial deployment (08:15 UTC) until mitigation was applied

## Resolution
The incident was resolved through a two-phase approach:
1. **Immediate Mitigation**: Increased pod memory limits from 512Mi to 1Gi and restarted all checkout-service pods to stabilize the service
2. **Permanent Fix**: Created PR #48 replacing the unbounded Map cache with an LRU cache (lru-cache package) configured with a maximum of 10,000 entries and 5-minute TTL, bounding memory usage to approximately 20MB under peak traffic

## Action Items
- [ ] Review and merge PR #48 with LRU cache implementation after validating memory behavior under sustained load in staging environment
- [ ] Add Datadog monitor for pod memory usage that alerts when consumption exceeds 70% of configured limit
- [ ] Audit codebase for similar unbounded Map or Object usage patterns in other services that could cause memory leaks
- [ ] Enable Node.js heap profiling (--inspect flag) in staging environment and establish periodic heap snapshot capture process
- [ ] Review and adjust memory limits and requests for checkout-service based on expected workload after fix deployment
- [ ] Update code review guidelines to require eviction policies for all in-memory cache implementations
- [ ] Add load testing requirements for PRs that introduce caching mechanisms

## Lessons Learned
- **In-memory caches require bounds**: Any in-memory cache implementation must include eviction policies, size limits, and TTLs to prevent unbounded growth. Unbounded Map/Object caches are a common source of memory leaks in Node.js applications.
- **Pre-production testing gaps**: The memory leak was not caught in staging, indicating that load testing did not adequately simulate production traffic patterns or duration. Sustained load tests are necessary to detect gradual memory growth.
- **Monitoring coverage**: Memory usage alerts at pod level would have provided early warning before OOMKill events occurred. Proactive monitoring at 70% threshold would enable intervention before service impact.
- **Deployment correlation**: The 2-hour delay between deployment (08:15 UTC) and incident detection (10:00 UTC) demonstrates the importance of monitoring memory trends immediately following deployments, especially for changes involving caching or data structures.
- **Code review process**: The PR introducing the unbounded cache should have been flagged during code review for lacking eviction logic. This indicates a need for stronger review guidelines around resource management patterns.$postmortem_2$,
  '2026-02-27 01:55:48',
  '2026-02-27 01:55:48'
)
ON CONFLICT (incident_id) DO NOTHING;
